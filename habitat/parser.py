# Copyright 2010, 2011, 2012 (C) Adam Greig, Daniel Richman
#
# This file is part of habitat.
#
# habitat is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# habitat is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with habitat.  If not, see <http://www.gnu.org/licenses/>.

"""
Interpret incoming telemetry strings into useful telemetry data.
"""

import base64
import logging
import hashlib
import M2Crypto
import os
import couchdbkit
import copy
import re
import json
import statsd

from . import loadable_manager
from .utils import dynamicloader

logger = logging.getLogger("habitat.parser")
statsd.init_statsd({'STATSD_BUCKET_PREFIX': 'habitat.parser'})

__all__ = ['Parser', 'ParserModule']


class Parser(object):
    """
    habitat's parser

    :class:`Parser` takes arbitrary unparsed  payload telemetry and
    attempts to use each loaded :class:`ParserModule` to turn this telemetry
    into useful data.
    """

    ascii_exp = re.compile("^[\\x20-\\x7E]+$")

    def __init__(self, config):
        """
        On construction, it will:

        * Use ``config[daemon_name]`` as ``self.config`` (defaults to
          'parser').
        * Load a :class:`habitat.loadable_manager.LoadableManager`, passing it
          *config*.
        * Load modules from ``self.config["modules"]``.
        * Scan ``self.config["certs_dir"]`` for CA and developer certificates.
        * Connects to CouchDB using ``self.config["couch_uri"]`` and
          ``config["couch_db"]``.
        """

        config = copy.deepcopy(config)
        parser_config = config["parser"]

        self.loadable_manager = loadable_manager.LoadableManager(config)

        self.modules = []

        for module in parser_config["modules"]:
            m = dynamicloader.load(module["class"])
            dynamicloader.expecthasmethod(m, "pre_parse")
            dynamicloader.expecthasmethod(m, "parse")
            dynamicloader.expecthasnumargs(m.pre_parse, 1)
            dynamicloader.expecthasnumargs(m.parse, 2)
            module["module"] = m(self)
            self.modules.append(module)

        self.certificate_authorities = []

        self.cert_path = parser_config["certs_dir"]
        ca_path = os.path.join(self.cert_path, 'ca')
        for f in os.listdir(ca_path):
            ca = M2Crypto.X509.load_cert(os.path.join(ca_path, f))
            if ca.check_ca():
                self.certificate_authorities.append(ca)
            else:
                raise ValueError("CA certificate is not a CA: " +
                    os.path.join(ca_path, f))

        self.loaded_certs = {}

        self.couch_server = couchdbkit.Server(config["couch_uri"])
        self.db = self.couch_server[config["couch_db"]]

    @statsd.StatsdTimer.wrap('time')
    def parse(self, doc):
        """
        Attempts to parse telemetry information out of a new telemetry
        document *doc*.

        This function attempts to determine which of the loaded parser
        modules should be used to parse the message, and which config
        file it should be given to do so.

        For the priority ordered list self.modules, resolution proceeds as::

            for module in modules:
                module.pre_parse to find a callsign
                if a callsign is found:
                    look up the configuration document for that callsign
                    if a configuration document is found:
                        check it specifies that this module should be used
                        if it does:
                            module.parse to get the data
                            return
            if we can't get any data:
                error

        Note that in the loops below, the filter, pre_parse, and
        parse methods will all raise a ValueError if failure occurs,
        continuing the loop.

        Once parsed, documents are saved back to the CouchDB database with the
        newly parsed data included. Some reserved field names will also be
        saved, which are indicated by a leading underscore.

        These fields may include:

        * ``_protocol`` which gives the parser module name that was used to
          decode this message

        From the UKHAS parser module in particular:

        * ``_sentence`` gives the ASCII sentence from the UKHAS parser
        * ``_extra_data`` from the UKHAS parser, where the sentence contained
          more data than the UKHAS parser was configured for

        Parser modules should be wary when outputting field names with
        leading underscores.
        """
        data = None
        try:
            original_data = doc['data']['_raw']
            receiver_callsign = doc['receivers'].keys()[0]
            time_created = doc['receivers'][receiver_callsign]['time_created']
        except (KeyError, IndexError) as e:
            logger.warning("Could not find required key in doc: " + str(e))
            statsd.increment("bad_doc")
            return None
        raw_data = base64.b64decode(original_data)

        if self.ascii_exp.search(raw_data):
            debug_type = 'ascii'
            debug_data = raw_data
            statsd.increment("ascii_doc")
        else:
            debug_type = 'b64'
            debug_data = original_data
            statsd.increment("binary_doc")

        logger.info("Parsing [{type}] {data!r} ({id})"\
                .format(id=doc["_id"], data=debug_data, type=debug_type))

        for module in self.modules:
            try:
                where = "pre_filter"
                data = self._pre_filter(raw_data, module)
                where = "pre_parse"
                callsign = module["module"].pre_parse(data)
            except (ValueError, KeyError) as e:
                logger.debug("Exception in {module} {where}: {e}"
                        .format(e=e, module=module['name'], where=where))
                statsd.increment("parse_exception")
                continue

            config_doc = self._find_config_doc(callsign, time_created)
            if not config_doc:
                logger.debug("No configuration doc for {callsign!r} found"
                        .format(callsign=callsign))
                statsd.increment("no_config_doc")
                continue

            config = config_doc["payloads"][callsign]
            if config["sentence"]["protocol"] != module["name"]:
                logger.debug("Incorrect protocol: {callsign},{module}"
                        .format(callsign=callsign, module=module["name"]))
                continue

            try:
                where = "intermediate filter"
                data = self._intermediate_filter(data, config)
                where = "main parse"
                data = module["module"].parse(data, config["sentence"])
                where = "post filter"
                data = self._post_filter(data, config)
            except (ValueError, KeyError) as e:
                logger.debug("Exception in {module} {where}: {e}"
                        .format(module=module['name'], e=e, where=where))
                statsd.increment("parse_exception")
                continue

            data["_protocol"] = module["name"]
            data["_flight"] = config_doc["_id"]
            data["_parsed"] = True
            break

        if type(data) is dict:
            doc['data'].update(data)
            logger.info("{module} parsed data from {callsign} successfully" \
                .format(module=module["name"], callsign=callsign))
            logger.debug("Parsed data: " + json.dumps(data))
            statsd.increment("parsed")
            if "_protocol" in data:
                statsd.increment("protocol.{0}".format(data['_protocol']))
            return doc
        else:
            logger.info("All attempts to parse failed")
            statsd.increment("failed")
            return None

    def _find_config_doc(self, callsign, time_created):
        """
        Check Couch for a configuration document we can use for this payload.
        The Couch view first tries to find any Flight documents with this
        callsign in their payloads dictionary, but will also return any
        Sandbox documents with this payload if no valid Flight documents
        could be found. Flight documents only count if their end time is
        in the future.
        If no config can be found, raises
        :py:exc:`ValueError <exceptions.ValueError>`. Otherwise, the whole
        flight document is returned.
        """

        startkey = [callsign, time_created]
        result = self.db.view("habitat/payload_config", limit=1,
                include_docs=True, startkey=startkey).first()

        if not result or callsign not in result["doc"]["payloads"]:
            return False
        else:
            return result["doc"]

    def _pre_filter(self, data, module):
        """
        Apply all the module's pre filters, in order, to the data and
        return the resulting filtered data.
        """
        if "pre-filters" in module:
            for f in module["pre-filters"]:
                data = self._filter(data, f, str)
                statsd.increment("filters.pre")
        return data

    def _intermediate_filter(self, data, config):
        """
        Apply all the intermediate (between getting the callsign and parsing)
        filters specified in the payload's configuration document and return
        the resulting filtered data.
        """
        if "filters" in config:
            if "intermediate" in config["filters"]:
                for f in config["filters"]["intermediate"]:
                    data = self._filter(data, f, str)
                    statsd.increment("filters.intermediate")
        return data

    def _post_filter(self, data, config):
        """
        Apply all the post (after parsing) filters specified in the payload's
        configuration document and return the resulting filtered data.
        """
        if "filters" in config:
            if "post" in config["filters"]:
                for f in config["filters"]["post"]:
                    data = self._filter(data, f, dict)
                    statsd.increment("filters.post")
        return data

    def _filter(self, data, f, result_type):
        """
        Load and run a filter from a dictionary specifying type, the
        relevant filter/code and maybe a config.
        Returns the filtered data, or leaves the data untouched
        if the filter could not be run.
        """

        rollback = data
        data = copy.deepcopy(data)

        try:
            if f["type"] == "normal":
                fil = 'filters.' + f['filter']
                data = self.loadable_manager.run(fil, f, data)
            elif f["type"] == "hotfix":
                data = self._hotfix_filter(data, f)
            else:
                raise ValueError("Invalid filter type")

            if not data or not isinstance(data, result_type):
                raise ValueError("Hotfix returned no output or "
                                 "output of wrong type")
        except:
            logger.exception("Error while applying filter " + repr(f))
            return rollback
        else:
            return data

    def _sanity_check_hotfix(self, f):
        """Perform basic sanity checks on **f**"""
        if "code" not in f:
            raise ValueError("Hotfix didn't have any code")
        if "signature" not in f:
            raise ValueError("Hotfix didn't have a signature")
        if "certificate" not in f:
            raise ValueError("Hotfix didn't specify a certificate")
        if os.path.basename(f["certificate"]) != f["certificate"]:
            raise ValueError("Hotfix's specified certificate was invalid")

    def _verify_certificate(self, f, cert):
        """Check that the certificate is cryptographically signed by a key
        which is signed by a known CA."""
        # Check the certificate is valid
        for ca_cert in self.certificate_authorities:
            if cert.verify(ca_cert.get_pubkey()):
                break
            raise ValueError("Certificate is not signed by a recognised CA.")

        # Check the signature is valid
        try:
            digest = hashlib.sha256(f["code"]).hexdigest()
            sig = base64.b64decode(f["signature"])
            ok = cert.get_pubkey().get_rsa().verify(digest, sig, 'sha256')
        except (TypeError, M2Crypto.RSA.RSAError):
            statsd.increment("filters.hotfix.invalid_signature")
            raise ValueError("Hotfix signature is not valid")
        if not ok:
            statsd.increment("filters.hotfix.invalid_signature")
            raise ValueError("Hotfix signature is not valid")

    def _compile_hotfix(self, f):
        """Compile a hotfix into a function **f** in an empty namespace."""
        logger.debug("Compiling a hotfix")
        body = "def f(data):\n"
        env = {}
        try:
            body += "\n".join("  " + l + "\n" for l in f["code"].split("\n"))
            code = compile(body, "<filter>", "exec")
            exec code in env
        except (SyntaxError, AttributeError, TypeError):
            statsd.increment("filters.hotfix.compile_error")
            raise ValueError("Hotfix code didn't compile: " + repr(f))
        return env

    def _hotfix_filter(self, data, f):
        """Load a filter specified by some code in the database. Check its
        authenticity by verifying its certificate, then run if OK."""
        # Check the hotfix has all the right fields
        self._sanity_check_hotfix(f)

        # Load requested certificate
        cert = self._get_certificate(f["certificate"])

        # Check the certificate and signature are cryptographically okay
        self._verify_certificate(f, cert)

        # Compile the hotfix
        env = self._compile_hotfix(f)

        logger.debug("Executing a hotfix")
        statsd.increment("filters.hotfix.executed")

        return env["f"](data)

    def _get_certificate(self, certname):
        """Fetch the specified certificate, returning the X509 object.
        Uses an instance cache to prevent too much filesystem I/O."""
        if certname in self.loaded_certs:
            return self.loaded_certs[certname]
        cert_path = os.path.join(self.cert_path, "certs", certname)
        if os.path.exists(cert_path):
            try:
                cert = M2Crypto.X509.load_cert(cert_path)
            except (IOError, M2Crypto.X509.X509Error):
                raise ValueError("Certificate could not be loaded.")
            self.loaded_certs[certname] = cert
            return cert
        else:
            raise ValueError("Certificate could not be loaded.")


class ParserModule(object):
    """
    Base class for real ParserModules to inherit from.

    **ParserModules** are classes which turn radio strings into useful data.
    They do not have to inherit from :class:`ParserModule`, but can if they
    want. They must implement :meth:`pre_parse` and :meth:`parse` as described
    below.
    """
    def __init__(self, parser):
        self.parser = parser
        self.loadable_manager = parser.loadable_manager

    def pre_parse(self, string):
        """
        Go though *string* and attempt to extract a callsign, returning
        it as a string. If no callsign could be extracted, a
        :exc:`ValueError <exceptions.ValueError>` is raised.
        """
        raise ValueError()

    def parse(self, string, config):
        """
        Go through *string* which has been identified as the format this
        parser module should be able to parse, extracting the data as per
        the information in *config*, which is the ``sentence`` dictionary
        extracted from the payload's configuration document.
        """
        raise ValueError()
