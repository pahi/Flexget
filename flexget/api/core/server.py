from __future__ import unicode_literals, division, absolute_import
from builtins import *  # noqa pylint: disable=unused-import, redefined-builtin
import copy

import base64

import os
import json
import sys
import logging
import threading
import traceback
from time import sleep

import binascii
import cherrypy
import yaml
from flask import Response, jsonify, request
from flask_restplus import inputs
from flexget.utils.tools import get_latest_flexget_version_number
from pyparsing import Word, Keyword, Group, Forward, Suppress, OneOrMore, oneOf, White, restOfLine, ParseException, \
    Combine
from pyparsing import nums, alphanums, printables
from yaml.error import YAMLError

from flexget._version import __version__
from flexget.api import api, APIResource
from flexget.api.app import __version__ as __api_version__, APIError, BadRequest, base_message, success_response, \
    base_message_schema, \
    empty_response, etag

log = logging.getLogger('api.server')

server_api = api.namespace('server', description='Manage Daemon')


class ObjectsContainer(object):
    yaml_error_response = copy.deepcopy(base_message)
    yaml_error_response['properties']['column'] = {'type': 'integer'}
    yaml_error_response['properties']['line'] = {'type': 'integer'}
    yaml_error_response['properties']['reason'] = {'type': 'string'}

    config_validation_error = copy.deepcopy(base_message)
    config_validation_error['properties']['error'] = {'type': 'string'}
    config_validation_error['properties']['config_path'] = {'type': 'string'}

    pid_object = {
        'type': 'object',
        'properties': {
            'pid': {'type': 'integer'}
        }
    }

    raw_config_object = {
        'type': 'object',
        'properties': {
            'raw_config': {'type': 'string'}
        }
    }

    version_object = {
        'type': 'object',
        'properties': {
            'flexget_version': {'type': 'string'},
            'api_version': {'type': 'string'},
            'latest_version': {'type': ['string', 'null']}
        }
    }

    dump_threads_object = {
        'type': 'object',
        'properties': {
            'threads': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string'},
                        'id': {'type': 'string'},
                        'dump': {
                            'type': 'array',
                            'items': {'type': 'string'}
                        }
                    },
                },
            }
        }
    }


yaml_error_schema = api.schema('yaml_error_schema', ObjectsContainer.yaml_error_response)
config_validation_schema = api.schema('config_validation_schema', ObjectsContainer.config_validation_error)
pid_schema = api.schema('server.pid', ObjectsContainer.pid_object)
raw_config_schema = api.schema('raw_config', ObjectsContainer.raw_config_object)
version_schema = api.schema('server.version', ObjectsContainer.version_object)
dump_threads_schema = api.schema('server.dump_threads', ObjectsContainer.dump_threads_object)


@server_api.route('/reload/')
class ServerReloadAPI(APIResource):
    @api.response(501, model=yaml_error_schema, description='YAML syntax error')
    @api.response(502, model=config_validation_schema, description='Config validation error')
    @api.response(200, model=base_message_schema, description='Newly reloaded config')
    def get(self, session=None):
        """ Reload Flexget config """
        log.info('Reloading config from disk.')
        try:
            self.manager.load_config(output_to_console=False)
        except YAMLError as e:
            if hasattr(e, 'problem') and hasattr(e, 'context_mark') and hasattr(e, 'problem_mark'):
                error = {}
                if e.problem is not None:
                    error.update({'reason': e.problem})
                if e.context_mark is not None:
                    error.update({'line': e.context_mark.line, 'column': e.context_mark.column})
                if e.problem_mark is not None:
                    error.update({'line': e.problem_mark.line, 'column': e.problem_mark.column})
                raise APIError(message='Invalid YAML syntax', payload=error)
        except ValueError as e:
            errors = []
            for er in e.errors:
                errors.append({'error': er.message,
                               'config_path': er.json_pointer})
            raise APIError('Error loading config: %s' % e.args[0], payload={'errors': errors})
        return success_response('Config successfully reloaded from disk')


@server_api.route('/pid/')
class ServerPIDAPI(APIResource):
    @api.response(200, description='Reloaded config', model=pid_schema)
    def get(self, session=None):
        """ Get server PID """
        return jsonify({'pid': os.getpid()})


shutdown_parser = api.parser()
shutdown_parser.add_argument('force', type=inputs.boolean, default=False, help='Ignore tasks in the queue')


@server_api.route('/shutdown/')
class ServerShutdownAPI(APIResource):
    @api.doc(parser=shutdown_parser)
    @api.response(200, model=base_message_schema, description='Shutdown requested')
    def get(self, session=None):
        """ Shutdown Flexget Daemon """
        args = shutdown_parser.parse_args()
        self.manager.shutdown(args['force'])
        return success_response('Shutdown requested')


@server_api.route('/config/')
class ServerConfigAPI(APIResource):
    @etag
    @api.response(200, description='Flexget config', model=empty_response)
    def get(self, session=None):
        """ Get Flexget Config in JSON form"""
        return jsonify(self.manager.config)


@server_api.route('/raw_config/')
class ServerRawConfigAPI(APIResource):
    @etag
    @api.doc(description='Return config file encoded in Base64')
    @api.response(200, model=raw_config_schema, description='Flexget raw YAML config file encoded in Base64')
    def get(self, session=None):
        """ Get raw YAML config file """
        with open(self.manager.config_path, 'r', encoding='utf-8') as f:
            raw_config = base64.b64encode(f.read().encode("utf-8"))
        return jsonify(raw_config=raw_config.decode('utf-8'))

    @api.validate(raw_config_schema)
    @api.response(200, model=base_message_schema, description='Successfully updated config')
    @api.response(BadRequest)
    @api.response(APIError)
    @api.doc(description='Config file must be base64 encoded. A backup will be created, and if successful config will'
                         ' be loaded and saved to original file.')
    def post(self, session=None):
        """ Update config """
        data = request.json
        try:
            raw_config = base64.b64decode(data['raw_config'])
        except (TypeError, binascii.Error):
            raise BadRequest(message='payload was not a valid base64 encoded string')

        try:
            config = yaml.safe_load(raw_config)
        except YAMLError as e:
            if hasattr(e, 'problem') and hasattr(e, 'context_mark') and hasattr(e, 'problem_mark'):
                error = {}
                if e.problem is not None:
                    error.update({'reason': e.problem})
                if e.context_mark is not None:
                    error.update({'line': e.context_mark.line, 'column': e.context_mark.column})
                if e.problem_mark is not None:
                    error.update({'line': e.problem_mark.line, 'column': e.problem_mark.column})
                raise BadRequest(message='Invalid YAML syntax', payload=error)

        try:
            backup_path = self.manager.update_config(config)
        except ValueError as e:
            errors = []
            for er in e.errors:
                errors.append({'error': er.message,
                               'config_path': er.json_pointer})
            raise BadRequest(message='Error loading config: %s' % e.args[0], payload={'errors': errors})

        try:
            self.manager.backup_config()
        except Exception as e:
            raise APIError(message='Failed to create config backup, config updated but NOT written to file',
                           payload={'reason': str(e)})

        try:
            with open(self.manager.config_path, 'w', encoding='utf-8') as f:
                f.write(raw_config.decode('utf-8').replace('\r\n', '\n'))
        except Exception as e:
            raise APIError(message='Failed to write new config to file, please load from backup',
                           payload={'reason': str(e), 'backup_path': backup_path})
        return success_response('Config was loaded and successfully updated to file')


@server_api.route('/version/')
@api.doc(description='In case of a request error when fetching latest flexget version, that value will return as null')
class ServerVersionAPI(APIResource):
    @api.response(200, description='Flexget version', model=version_schema)
    def get(self, session=None):
        """ Flexget Version """
        latest = get_latest_flexget_version_number()
        return jsonify({'flexget_version': __version__,
                        'api_version': __api_version__,
                        'latest_version': latest})


@server_api.route('/dump_threads/', doc=False)
class ServerDumpThreads(APIResource):
    @api.response(200, description='Flexget threads dump', model=dump_threads_schema)
    def get(self, session=None):
        """ Dump Server threads for debugging """
        id2name = dict([(th.ident, th.name) for th in threading.enumerate()])
        threads = []
        for threadId, stack in sys._current_frames().items():
            dump = []
            for filename, lineno, name, line in traceback.extract_stack(stack):
                dump.append('File: "%s", line %d, in %s' % (filename, lineno, name))
                if line:
                    dump.append(line.strip())
            threads.append({
                'name': id2name.get(threadId),
                'id': threadId,
                'dump': dump
            })

        return jsonify(threads=threads)


server_log_parser = api.parser()
server_log_parser.add_argument('lines', type=int, default=200, help='How many lines to find before streaming')
server_log_parser.add_argument('search', help='Search filter support google like syntax')


def reverse_readline(fh, start_byte=0, buf_size=8192):
    """a generator that returns the lines of a file in reverse order"""
    segment = None
    offset = 0
    if start_byte:
        fh.seek(start_byte)
    else:
        fh.seek(0, os.SEEK_END)
    total_size = remaining_size = fh.tell()
    while remaining_size > 0:
        offset = min(total_size, offset + buf_size)
        fh.seek(-offset, os.SEEK_END)
        buf = fh.read(min(remaining_size, buf_size))
        remaining_size -= buf_size
        lines = buf.decode(sys.getfilesystemencoding()).split('\n')
        # the first line of the buffer is probably not a complete line so
        # we'll save it and append it to the last line of the next buffer
        # we read
        if segment is not None:
            # if the previous chunk starts right from the beginning of line
            # do not concact the segment to the last line of new chunk
            # instead, yield the segment first
            if buf[-1] is not '\n':
                lines[-1] += segment
            else:
                yield segment
        segment = lines[0]
        for index in range(len(lines) - 1, 0, -1):
            if len(lines[index]):
                yield lines[index]
    yield segment


def file_inode(filename):
    try:
        fd = os.open(filename, os.O_RDONLY)
        inode = os.fstat(fd).st_ino
        return inode
    except OSError:
        return 0
    finally:
        if fd:
            os.close(fd)


@server_api.route('/log/')
class ServerLogAPI(APIResource):
    @api.doc(parser=server_log_parser)
    @api.response(200, description='Streams as line delimited JSON')
    def get(self, session=None):
        """ Stream Flexget log Streams as line delimited JSON """
        args = server_log_parser.parse_args()

        def follow(lines, search):
            log_parser = LogParser(search)
            stream_from_byte = 0

            lines_found = []

            if os.path.isabs(self.manager.options.logfile):
                base_log_file = self.manager.options.logfile
            else:
                base_log_file = os.path.join(self.manager.config_base, self.manager.options.logfile)

            yield '{"stream": ['  # Start of the json stream

            # Read back in the logs until we find enough lines
            for i in range(0, 9):
                log_file = ('%s.%s' % (base_log_file, i)).rstrip('.0')  # 1st log file has no number

                if not os.path.isfile(log_file):
                    break

                with open(log_file, 'rb') as fh:
                    fh.seek(0, 2)  # Seek to bottom of file
                    end_byte = fh.tell()
                    if i == 0:
                        stream_from_byte = end_byte  # Stream from this point later on

                    if len(lines_found) >= lines:
                        break

                    # Read in reverse for efficiency
                    for line in reverse_readline(fh, start_byte=end_byte):
                        if len(lines_found) >= lines:
                            break
                        if log_parser.matches(line):
                            lines_found.append(log_parser.json_string(line))

                    for l in reversed(lines_found):
                        yield l + ',\n'

            # We need to track the inode in case the log file is rotated
            current_inode = file_inode(base_log_file)

            while True:
                # If the server is shutting down then end the stream nicely
                if cherrypy.engine.state != cherrypy.engine.states.STARTED:
                    break

                new_inode = file_inode(base_log_file)
                if current_inode != new_inode:
                    # File updated/rotated. Read from beginning
                    stream_from_byte = 0
                    current_inode = new_inode

                try:
                    with open(base_log_file, 'rb') as fh:
                        fh.seek(stream_from_byte)
                        line = fh.readline().decode(sys.getfilesystemencoding())
                        stream_from_byte = fh.tell()
                except IOError:
                    yield '{}'
                    continue

                # If a valid line is found and does not pass the filter then set it to none
                line = log_parser.json_string(line) if log_parser.matches(line) else '{}'

                if line == '{}':
                    # If no match then delay to prevent many read hits on the file
                    sleep(2)

                yield line + ',\n'

            yield '{}]}'  # End of stream

        return Response(follow(args['lines'], args['search']), mimetype='text/event-stream')


class LogParser(object):
    """
    Filter log file.

    Supports
      * 'and', 'or' and implicit 'and' operators;
      * parentheses;
      * quoted strings;
    """

    def __init__(self, query):
        self._methods = {
            'and': self.evaluate_and,
            'or': self.evaluate_or,
            'not': self.evaluate_not,
            'parenthesis': self.evaluate_parenthesis,
            'quotes': self.evaluate_quotes,
            'word': self.evaluate_word,
        }

        self.line = ''
        self.query = query.lower() if query else ''

        if self.query:
            # TODO: Cleanup
            operator_or = Forward()
            operator_word = Group(Word(alphanums)).setResultsName('word')

            operator_quotes_content = Forward()
            operator_quotes_content << (
                (operator_word + operator_quotes_content) | operator_word
            )

            operator_quotes = Group(
                Suppress('"') + operator_quotes_content + Suppress('"')
            ).setResultsName('quotes') | operator_word

            operator_parenthesis = Group(
                (Suppress('(') + operator_or + Suppress(")"))
            ).setResultsName('parenthesis') | operator_quotes

            operator_not = Forward()
            operator_not << (Group(
                Suppress(Keyword('no', caseless=True)) + operator_not
            ).setResultsName('not') | operator_parenthesis)

            operator_and = Forward()
            operator_and << (Group(
                operator_not + Suppress(Keyword('and', caseless=True)) + operator_and
            ).setResultsName('and') | Group(
                operator_not + OneOrMore(~oneOf('and or') + operator_and)
            ).setResultsName('and') | operator_not)

            operator_or << (Group(
                operator_and + Suppress(Keyword('or', caseless=True)) + operator_or
            ).setResultsName('or') | operator_and)

            self._query_parser = operator_or.parseString(self.query)[0]
        else:
            self._query_parser = False

        time_cmpnt = Word(nums).setParseAction(lambda t: t[0].zfill(2))
        date = Combine((time_cmpnt + '-' + time_cmpnt + '-' + time_cmpnt) + ' ' + time_cmpnt + ':' + time_cmpnt)
        word = Word(printables)

        self._log_parser = (
            date.setResultsName('timestamp') +
            word.setResultsName('log_level') +
            word.setResultsName('plugin') +
            (
                White(min=16).setParseAction(lambda s, l, t: [t[0].strip()]).setResultsName('task') |
                (White(min=1).suppress() & word.setResultsName('task'))
            ) +
            restOfLine.setResultsName('message')
        )

    def evaluate_and(self, argument):
        return self.evaluate(argument[0]) and self.evaluate(argument[1])

    def evaluate_or(self, argument):
        return self.evaluate(argument[0]) or self.evaluate(argument[1])

    def evaluate_not(self, argument):
        return not self.evaluate(argument[0])

    def evaluate_parenthesis(self, argument):
        return self.evaluate(argument[0])

    def evaluate_quotes(self, argument):
        search_terms = [term[0] for term in argument]
        return ' '.join(search_terms) in ' '.join(self.line.split())

    def evaluate_word(self, argument):
        return argument[0] in self.line

    def evaluate(self, argument):
        return self._methods[argument.getName()](argument)

    def matches(self, line):
        if not line:
            return False

        self.line = line.lower()

        if not self._query_parser:
            return True
        else:
            return self.evaluate(self._query_parser)

    def json_string(self, line):
        try:
            return json.dumps(self._log_parser().parseString(line).asDict())
        except ParseException:
            return '{}'