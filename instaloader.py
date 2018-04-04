#!/usr/bin/env python3

"""Download pictures (or videos) along with their captions and other metadata from Instagram."""

import ast
import getpass
import json
import os
import pickle
import platform
import random
import re
import shutil
import string
import sys
import tempfile
import textwrap
import time
import urllib.parse
from argparse import ArgumentParser, SUPPRESS
from base64 import b64decode, b64encode
from contextlib import contextmanager, suppress
from datetime import datetime
from enum import Enum
from functools import wraps
from io import BytesIO
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests
import requests.utils
import urllib3


__version__ = '3.3.4'

# NOTE: duplicated in README.rst and docs/index.rst
USAGE_STRING = """
{0} [--comments] [--geotags] [--stories]
{2:{1}} [--login YOUR-USERNAME] [--fast-update]
{2:{1}} profile | "#hashtag" | :stories | :feed | :saved
{0} --help""".format(sys.argv[0], len(sys.argv[0]), '')

try:
    # pylint:disable=wrong-import-position
    import win_unicode_console
except ImportError:
    WINUNICODE = False
else:
    win_unicode_console.enable()
    WINUNICODE = True


class InstaloaderException(Exception):
    """Base exception for this script.

    :note: This exception should not be raised directly."""
    pass


class QueryReturnedNotFoundException(InstaloaderException):
    pass


class QueryReturnedForbiddenException(InstaloaderException):
    pass


class ProfileNotExistsException(InstaloaderException):
    pass


class ProfileHasNoPicsException(InstaloaderException):
    pass


class PrivateProfileNotFollowedException(InstaloaderException):
    pass


class LoginRequiredException(InstaloaderException):
    pass


class InvalidArgumentException(InstaloaderException):
    pass


class BadResponseException(InstaloaderException):
    pass


class BadCredentialsException(InstaloaderException):
    pass


class ConnectionException(InstaloaderException):
    pass


class TooManyRequestsException(ConnectionException):
    pass


def get_default_session_filename(username: str) -> str:
    """Returns default session filename for given username."""
    dirname = tempfile.gettempdir() + "/" + ".instaloader-" + getpass.getuser()
    filename = dirname + "/" + "session-" + username
    return filename.lower()


def copy_session(session: requests.Session) -> requests.Session:
    """Duplicates a requests.Session."""
    new = requests.Session()
    new.cookies = \
        requests.utils.cookiejar_from_dict(requests.utils.dict_from_cookiejar(session.cookies))
    new.headers = session.headers.copy()
    return new


def default_user_agent() -> str:
    return 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ' \
           '(KHTML, like Gecko) Chrome/51.0.2704.79 Safari/537.36'


def shortcode_to_mediaid(code: str) -> int:
    if len(code) > 11:
        raise InvalidArgumentException("Wrong shortcode \"{0}\", unable to convert to mediaid.".format(code))
    code = 'A' * (12 - len(code)) + code
    return int.from_bytes(b64decode(code.encode(), b'-_'), 'big')


def mediaid_to_shortcode(mediaid: int) -> str:
    if mediaid.bit_length() > 64:
        raise InvalidArgumentException("Wrong mediaid {0}, unable to convert to shortcode".format(str(mediaid)))
    return b64encode(mediaid.to_bytes(9, 'big'), b'-_').decode().replace('A', ' ').lstrip().replace(' ','A')


def format_string_contains_key(format_string: '_PathPattern', key: str) -> bool:
    # pylint:disable=unused-variable
    for literal_text, field_name, format_spec, conversion in string.Formatter().parse(format_string):
        if field_name and (field_name == key or field_name.startswith(key + '.')):
            return True
    return False


def filterstr_to_filterfunc(filter_str: str, logged_in: bool) -> Callable[['Post'], bool]:
    """Takes an --only-if=... filter specification and makes a filter_func Callable out of it."""

    # The filter_str is parsed, then all names occurring in its AST are replaced by loads to post.<name>. A
    # function Post->bool is returned which evaluates the filter with the post as 'post' in its namespace.

    class TransformFilterAst(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name):
            # pylint:disable=no-self-use
            if not isinstance(node.ctx, ast.Load):
                raise InvalidArgumentException("Invalid filter: Modifying variables ({}) not allowed.".format(node.id))
            if not hasattr(Post, node.id):
                raise InvalidArgumentException("Invalid filter: Name {} is not defined.".format(node.id))
            if node.id in Post.LOGIN_REQUIRING_PROPERTIES and not logged_in:
                raise InvalidArgumentException("Invalid filter: Name {} requires being logged in.".format(node.id))
            new_node = ast.Attribute(ast.copy_location(ast.Name('post', ast.Load()), node), node.id,
                                     ast.copy_location(ast.Load(), node))
            return ast.copy_location(new_node, node)

    input_filename = '<--only-if parameter>'
    compiled_filter = compile(TransformFilterAst().visit(ast.parse(filter_str, filename=input_filename, mode='eval')),
                              filename=input_filename, mode='eval')

    def filterfunc(post: 'Post') -> bool:
        # pylint:disable=eval-used
        return bool(eval(compiled_filter, {'post': post}))

    return filterfunc


class InstaloaderContext:
    """Class providing methods for (error) logging and low-level communication with Instagram.

    It is not thought to be instantiated directly, rather :class:`Instaloader` instances maintain a context
    object.

    For logging, it provides :meth:`log`, :meth:`error`, :meth:`error_catcher`.

    It provides low-level communication routines :meth:`get_json`, :meth:`graphql_query`, :meth:`graphql_node_list`,
    :meth:`get_and_write_raw` and implements mechanisms for rate controlling and error handling.

    Further, it provides methods for logging in and general session handles, which are used by that routines in
    class :class:`Instaloader`.
    """

    def __init__(self, sleep: bool = True, quiet: bool = False,
                 user_agent: Optional[str] = None, max_connection_attempts: int = 3):

        self.user_agent = user_agent if user_agent is not None else default_user_agent()
        self._session = self.get_anonymous_session()
        self.username = None
        self.sleep = sleep
        self.quiet = quiet
        self.max_connection_attempts = max_connection_attempts

        # error log, filled with error() and printed at the end of Instaloader.main()
        self.error_log = []

        # For the adaption of sleep intervals (rate control)
        self.previous_queries = dict()

        # Can be set to True for testing, disables supression of InstaloaderContext._error_catcher
        self.raise_all_errors = False

    @property
    def is_logged_in(self) -> bool:
        """True, if this Instaloader instance is logged in."""
        return bool(self.username)

    def log(self, *msg, sep='', end='\n', flush=False):
        """Log a message to stdout that can be suppressed with --quiet."""
        if not self.quiet:
            print(*msg, sep=sep, end=end, flush=flush)

    def error(self, msg, repeat_at_end = True):
        """Log a non-fatal error message to stderr, which is repeated at program termination.

        :param repeat_at_end: Set to false if the message should be printed, but not repeated at program termination."""
        print(msg, file=sys.stderr)
        if repeat_at_end:
            self.error_log.append(msg)

    def close(self):
        """Print error log and close session"""
        if self.error_log and not self.quiet:
            print("\nErrors occured:", file=sys.stderr)
            for err in self.error_log:
                print(err, file=sys.stderr)
        self._session.close()

    @contextmanager
    def error_catcher(self, extra_info: Optional[str] = None):
        """
        Context manager to catch, print and record InstaloaderExceptions.

        :param extra_info: String to prefix error message with."""
        try:
            yield
        except InstaloaderException as err:
            if extra_info:
                self.error('{}: {}'.format(extra_info, err))
            else:
                self.error('{}'.format(err))
            if self.raise_all_errors:
                raise err

    def _default_http_header(self, empty_session_only: bool = False) -> Dict[str, str]:
        """Returns default HTTP header we use for requests."""
        header = {'Accept-Encoding': 'gzip, deflate',
                  'Accept-Language': 'en-US,en;q=0.8',
                  'Connection': 'keep-alive',
                  'Content-Length': '0',
                  'Host': 'www.instagram.com',
                  'Origin': 'https://www.instagram.com',
                  'Referer': 'https://www.instagram.com/',
                  'User-Agent': self.user_agent,
                  'X-Instagram-AJAX': '1',
                  'X-Requested-With': 'XMLHttpRequest'}
        if empty_session_only:
            del header['Host']
            del header['Origin']
            del header['Referer']
            del header['X-Instagram-AJAX']
            del header['X-Requested-With']
        return header

    def get_anonymous_session(self) -> requests.Session:
        """Returns our default anonymous requests.Session object."""
        session = requests.Session()
        session.cookies.update({'sessionid': '', 'mid': '', 'ig_pr': '1',
                                'ig_vw': '1920', 'csrftoken': '',
                                's_network': '', 'ds_user_id': ''})
        session.headers.update(self._default_http_header(empty_session_only=True))
        return session

    def save_session_to_file(self, sessionfile):
        pickle.dump(requests.utils.dict_from_cookiejar(self._session.cookies), sessionfile)

    def load_session_from_file(self, username, sessionfile):
        session = requests.Session()
        session.cookies = requests.utils.cookiejar_from_dict(pickle.load(sessionfile))
        session.headers.update(self._default_http_header())
        session.headers.update({'X-CSRFToken': session.cookies.get_dict()['csrftoken']})
        self._session = session
        self.username = username

    def test_login(self) -> Optional[str]:
        data = self.graphql_query("d6f4427fbe92d846298cf93df0b937d3", {})
        return data["data"]["user"]["username"] if data["data"]["user"] is not None else None

    def login(self, user, passwd):
        session = requests.Session()
        session.cookies.update({'sessionid': '', 'mid': '', 'ig_pr': '1',
                                'ig_vw': '1920', 'csrftoken': '',
                                's_network': '', 'ds_user_id': ''})
        session.headers.update(self._default_http_header())
        self._sleep()
        resp = session.get('https://www.instagram.com/')
        session.headers.update({'X-CSRFToken': resp.cookies['csrftoken']})
        self._sleep()
        login = session.post('https://www.instagram.com/accounts/login/ajax/',
                             data={'password': passwd, 'username': user}, allow_redirects=True)
        session.headers.update({'X-CSRFToken': login.cookies['csrftoken']})
        if login.status_code == 200:
            self._session = session
            if user == self.test_login():
                self.username = user
            else:
                self.username = None
                self._session = None
                raise BadCredentialsException('Login error! Check your credentials!')
        else:
            raise ConnectionException('Login error! Connection error!')

    def _sleep(self):
        """Sleep a short time if self.sleep is set. Called before each request to instagram.com."""
        if self.sleep:
            time.sleep(random.uniform(0.5, 3))

    def get_json(self, path: str, params: Dict[str, Any], host: str = 'www.instagram.com',
                 session: Optional[requests.Session] = None, _attempt = 1) -> Dict[str, Any]:
        """JSON request to Instagram.

        :param path: URL, relative to the given domain which defaults to www.instagram.com/
        :param params: GET parameters
        :param host: Domain part of the URL from where to download the requested JSON; defaults to www.instagram.com
        :param session: Session to use, or None to use self.session
        :return: Decoded response dictionary
        :raises QueryReturnedNotFoundException: When the server responds with a 404.
        :raises ConnectionException: When query repeatedly failed.
        """
        def graphql_query_waittime(query_hash: str, untracked_queries: bool = False) -> int:
            sliding_window = 660
            timestamps = self.previous_queries.get(query_hash)
            if not timestamps:
                return sliding_window if untracked_queries else 0
            current_time = time.monotonic()
            timestamps = list(filter(lambda t: t > current_time - sliding_window, timestamps))
            self.previous_queries[query_hash] = timestamps
            if len(timestamps) < 100 and not untracked_queries:
                return 0
            return round(min(timestamps) + sliding_window - current_time) + 6
        is_graphql_query = 'query_hash' in params and 'graphql/query' in path
        if is_graphql_query:
            query_hash = params['query_hash']
            waittime = graphql_query_waittime(query_hash)
            if waittime > 0:
                self.log('\nToo many queries in the last time. Need to wait {} seconds.'.format(waittime))
                time.sleep(waittime)
            timestamp_list = self.previous_queries.get(query_hash)
            if timestamp_list is not None:
                timestamp_list.append(time.monotonic())
            else:
                self.previous_queries[query_hash] = [time.monotonic()]
        sess = session if session else self._session
        try:
            self._sleep()
            resp = sess.get('https://{0}/{1}'.format(host, path), params=params, allow_redirects=False)
            while resp.is_redirect:
                redirect_url = resp.headers['location']
                self.log('\nHTTP redirect from https://{0}/{1} to {2}'.format(host, path, redirect_url))
                if redirect_url.index('https://{}/'.format(host)) == 0:
                    resp = sess.get(redirect_url if redirect_url.endswith('/') else redirect_url + '/',
                                    params=params, allow_redirects=False)
                else:
                    break
            if resp.status_code == 404:
                raise QueryReturnedNotFoundException("404")
            if resp.status_code == 429:
                raise TooManyRequestsException("429 - Too Many Requests")
            if resp.status_code != 200:
                raise ConnectionException("HTTP error code {}.".format(resp.status_code))
            resp_json = resp.json()
            if 'status' in resp_json and resp_json['status'] != "ok":
                if 'message' in resp_json:
                    raise ConnectionException("Returned \"{}\" status, message \"{}\".".format(resp_json['status'],
                                                                                               resp_json['message']))
                else:
                    raise ConnectionException("Returned \"{}\" status.".format(resp_json['status']))
            return resp_json
        except (ConnectionException, json.decoder.JSONDecodeError, requests.exceptions.RequestException) as err:
            error_string = "JSON Query to {}: {}".format(path, err)
            if _attempt == self.max_connection_attempts:
                raise ConnectionException(error_string)
            self.error(error_string + " [retrying; skip with ^C]", repeat_at_end=False)
            text_for_429 = ("HTTP error code 429 was returned because too many queries occured in the last time. "
                            "Please do not use Instagram in your browser or run multiple instances of Instaloader "
                            "in parallel.")
            try:
                if isinstance(err, TooManyRequestsException):
                    print(textwrap.fill(text_for_429), file=sys.stderr)
                    if is_graphql_query:
                        waittime = graphql_query_waittime(query_hash=params['query_hash'], untracked_queries=True)
                        if waittime > 0:
                            self.log('The request will be retried in {} seconds.'.format(waittime))
                            time.sleep(waittime)
                self._sleep()
                return self.get_json(path=path, params=params, host=host, session=sess, _attempt=_attempt + 1)
            except KeyboardInterrupt:
                self.error("[skipped by user]", repeat_at_end=False)
                raise ConnectionException(error_string)

    def graphql_query(self, query_hash: str, variables: Dict[str, Any],
                      referer: Optional[str] = None) -> Dict[str, Any]:
        """
        Do a GraphQL Query.

        :param query_hash: Query identifying hash.
        :param variables: Variables for the Query.
        :param referer: HTTP Referer, or None.
        :return: The server's response dictionary.
        """
        tmpsession = copy_session(self._session)
        tmpsession.headers.update(self._default_http_header(empty_session_only=True))
        del tmpsession.headers['Connection']
        del tmpsession.headers['Content-Length']
        tmpsession.headers['authority'] = 'www.instagram.com'
        tmpsession.headers['scheme'] = 'https'
        tmpsession.headers['accept'] = '*/*'
        if referer is not None:
            tmpsession.headers['referer'] = urllib.parse.quote(referer)
        resp_json = self.get_json('graphql/query',
                                  params={'query_hash': query_hash,
                                          'variables': json.dumps(variables, separators=(',', ':'))},
                                  session=tmpsession)
        tmpsession.close()
        if 'status' not in resp_json:
            self.error("GraphQL response did not contain a \"status\" field.")
        return resp_json

    def graphql_node_list(self, query_hash: str, query_variables: Dict[str, Any],
                          query_referer: Optional[str],
                          edge_extractor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        """Retrieve a list of GraphQL nodes."""
        query_variables['first'] = Instaloader.GRAPHQL_PAGE_LENGTH
        data = self.graphql_query(query_hash, query_variables, query_referer)
        while True:
            edge_struct = edge_extractor(data)
            yield from [edge['node'] for edge in edge_struct['edges']]
            if edge_struct['page_info']['has_next_page']:
                query_variables['after'] = edge_struct['page_info']['end_cursor']
                data = self.graphql_query(query_hash, query_variables, query_referer)
            else:
                break

    def get_and_write_raw(self, url: str, filename: str, _attempt = 1) -> None:
        """Downloads raw data.

        :raises QueryReturnedNotFoundException: When the server responds with a 404.
        :raises QueryReturnedForbiddenException: When the server responds with a 403.
        :raises ConnectionException: When download repeatedly failed."""
        try:
            with self.get_anonymous_session() as anonymous_session:
                resp = anonymous_session.get(url)
            if resp.status_code == 200:
                self.log(filename, end=' ', flush=True)
                with open(filename, 'wb') as file:
                    resp.raw.decode_content = True
                    shutil.copyfileobj(resp.raw, file)
            else:
                if resp.status_code == 403:
                    # suspected invalid URL signature
                    raise QueryReturnedForbiddenException("403 when accessing {}.".format(url))
                if resp.status_code == 404:
                    # 404 not worth retrying.
                    raise QueryReturnedNotFoundException("404 when accessing {}.".format(url))
                raise ConnectionException("HTTP error code {}.".format(resp.status_code))
        except (urllib3.exceptions.HTTPError, requests.exceptions.RequestException, ConnectionException) as err:
            error_string = "URL {}: {}".format(url, err)
            if _attempt == self.max_connection_attempts:
                raise ConnectionException(error_string)
            self.error(error_string + " [retrying; skip with ^C]", repeat_at_end=False)
            try:
                self._sleep()
                self.get_and_write_raw(url, filename, _attempt + 1)
            except KeyboardInterrupt:
                self.error("[skipped by user]", repeat_at_end=False)
                raise ConnectionException(error_string)


class Post:
    """
    Structure containing information about an Instagram post.

    Created by methods :meth:`Profile.get_posts`, :meth:`Instaloader.get_hashtag_posts`,
    :meth:`Instaloader.get_feed_posts` and :meth:`Profile.get_saved_posts`.
    This class unifies access to the properties associated with a post. It implements == and is
    hashable.

    The properties defined here are accessible by the filter expressions specified with the :option:`--only-if`
    parameter and exported into JSON files with :option:`--metadata-json`.
    """

    LOGIN_REQUIRING_PROPERTIES = ["viewer_has_liked"]

    def __init__(self, context: InstaloaderContext, node: Dict[str, Any],
                 owner_profile: Optional['Profile'] = None):
        """Create a Post instance from a node structure as returned by Instagram.

        :param context: :class:`InstaloaderContext` instance used for additional queries if neccessary.
        :param node: Node structure, as returned by Instagram.
        :param owner_profile: The Profile of the owner, if already known at creation.
        """
        self._context = context
        self._node = node
        self._owner_profile = owner_profile
        self._full_metadata_dict = None

    @classmethod
    def from_shortcode(cls, context: InstaloaderContext, shortcode: str):
        """Create a post object from a given shortcode"""
        # pylint:disable=protected-access
        post = cls(context, {'shortcode': shortcode})
        post._node = post._full_metadata
        return post

    @classmethod
    def from_mediaid(cls, context: InstaloaderContext, mediaid: int):
        """Create a post object from a given mediaid"""
        return cls.from_shortcode(context, mediaid_to_shortcode(mediaid))

    @property
    def shortcode(self) -> str:
        """Media shortcode. URL of the post is instagram.com/p/<shortcode>/."""
        return self._node['shortcode'] if 'shortcode' in self._node else self._node['code']

    @property
    def mediaid(self) -> int:
        """The mediaid is a decimal representation of the media shortcode."""
        return int(self._node['id'])

    def __repr__(self):
        return '<Post {}>'.format(self.shortcode)

    def __eq__(self, o: object) -> bool:
        if isinstance(o, Post):
            return self.shortcode == o.shortcode
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.shortcode)

    @property
    def _full_metadata(self) -> Dict[str, Any]:
        if not self._full_metadata_dict:
            pic_json = self._context.get_json("p/{0}/".format(self.shortcode), params={'__a': 1})
            if "graphql" in pic_json:
                self._full_metadata_dict = pic_json["graphql"]["shortcode_media"]
            else:
                self._full_metadata_dict = pic_json["media"]
        return self._full_metadata_dict

    def _field(self, *keys) -> Any:
        """Lookups given fields in _node, and if not found in _full_metadata. Raises KeyError if not found anywhere."""
        try:
            d = self._node
            for key in keys:
                d = d[key]
            return d
        except KeyError:
            d = self._full_metadata
            for key in keys:
                d = d[key]
            return d

    @property
    def owner_username(self) -> str:
        """The Post's lowercase owner name, or 'UNKNOWN'."""
        try:
            if self._owner_profile:
                return self._owner_profile.username.lower()
            return self._field('owner', 'username').lower()
        except (InstaloaderException, KeyError, TypeError) as err:
            if self._context.raise_all_errors:
                raise err
            self._context.error("Get owner name of {}: {} -- using \'UNKNOWN\'.".format(self, err))
            return 'UNKNOWN'

    @property
    def owner_id(self) -> int:
        """The ID of the Post's owner."""
        if self._owner_profile:
            return self._owner_profile.userid
        return int(self._field('owner', 'id'))

    @property
    def date_local(self) -> datetime:
        """Timestamp when the post was created (local time zone)."""
        return datetime.fromtimestamp(self._node["date"] if "date" in self._node else self._node["taken_at_timestamp"])

    @property
    def date_utc(self) -> datetime:
        """Timestamp when the post was created (UTC)."""
        return datetime.utcfromtimestamp(self._node["date"] if "date" in self._node else self._node["taken_at_timestamp"])

    @property
    def url(self) -> str:
        """URL of the picture / video thumbnail of the post"""
        return self._node["display_url"] if "display_url" in self._node else self._node["display_src"]

    @property
    def typename(self) -> str:
        """Type of post, GraphImage, GraphVideo or GraphSidecar"""
        if '__typename' in self._node:
            return self._node['__typename']
        # if __typename is not in node, it is an old image or video
        return 'GraphImage'

    def get_sidecar_edges(self) -> List[Dict[str, Any]]:
        return self._field('edge_sidecar_to_children', 'edges')

    @property
    def caption(self) -> Optional[str]:
        """Caption."""
        if "edge_media_to_caption" in self._node and self._node["edge_media_to_caption"]["edges"]:
            return self._node["edge_media_to_caption"]["edges"][0]["node"]["text"]
        elif "caption" in self._node:
            return self._node["caption"]

    @property
    def caption_hashtags(self) -> List[str]:
        """List of all lowercased hashtags (without preceeding #) that occur in the Post's caption."""
        if not self.caption:
            return []
        # This regular expression is from jStassen, adjusted to use Python's \w to support Unicode
        # http://blog.jstassen.com/2016/03/code-regex-for-instagram-username-and-hashtags/
        hashtag_regex = re.compile(r"(?:#)(\w(?:(?:\w|(?:\.(?!\.))){0,28}(?:\w))?)")
        return re.findall(hashtag_regex, self.caption.lower())

    @property
    def caption_mentions(self) -> List[str]:
        """List of all lowercased profiles that are mentioned in the Post's caption, without preceeding @."""
        if not self.caption:
            return []
        # This regular expression is from jStassen, adjusted to use Python's \w to support Unicode
        # http://blog.jstassen.com/2016/03/code-regex-for-instagram-username-and-hashtags/
        mention_regex = re.compile(r"(?:@)(\w(?:(?:\w|(?:\.(?!\.))){0,28}(?:\w))?)")
        return re.findall(mention_regex, self.caption.lower())

    @property
    def tagged_users(self) -> List[str]:
        """List of all lowercased users that are tagged in the Post."""
        try:
            return [edge['node']['user']['username' ].lower() for edge in self._field('edge_media_to_tagged_user',
                                                                                      'edges')]
        except KeyError:
            return []

    @property
    def is_video(self) -> bool:
        """True if the Post is a video."""
        return self._node['is_video']

    @property
    def video_url(self) -> Optional[str]:
        """URL of the video, or None."""
        if self.is_video:
            return self._field('video_url')

    @property
    def viewer_has_liked(self) -> Optional[bool]:
        """Whether the viewer has liked the post, or None if not logged in."""
        if not self._context.is_logged_in:
            return None
        if 'likes' in self._node and 'viewer_has_liked' in self._node['likes']:
            return self._node['likes']['viewer_has_liked']
        return self._field('viewer_has_liked')

    @property
    def likes(self) -> int:
        """Likes count"""
        return self._field('edge_media_preview_like', 'count')

    @property
    def comments(self) -> int:
        """Comment count"""
        return self._field('edge_media_to_comment', 'count')

    def get_comments(self) -> Iterator[Dict[str, Any]]:
        """Iterate over all comments of the post.

        Each comment is represented by a dictionary having the keys text, created_at, id and owner, which is a
        dictionary with keys username, profile_pic_url and id.
        """
        if self.comments == 0:
            # Avoid doing additional requests if there are no comments
            return
        comment_edges = self._field('edge_media_to_comment', 'edges')
        if self.comments == len(comment_edges):
            # If the Post's metadata already contains all comments, don't do GraphQL requests to obtain them
            yield from (comment['node'] for comment in comment_edges)
        yield from self._context.graphql_node_list("33ba35852cb50da46f5b5e889df7d159",
                                                   {'shortcode': self.shortcode},
                                                   'https://www.instagram.com/p/' + self.shortcode + '/',
                                                   lambda d: d['data']['shortcode_media']['edge_media_to_comment'])

    def get_likes(self) -> Iterator[Dict[str, Any]]:
        """Iterate over all likes of the post.

        Each like is represented by a dictionary having the keys username, followed_by_viewer, id, is_verified,
        requested_by_viewer, followed_by_viewer, profile_pic_url.
        """
        if self.likes == 0:
            # Avoid doing additional requests if there are no comments
            return
        likes_edges = self._field('edge_media_preview_like', 'edges')
        if self.likes == len(likes_edges):
            # If the Post's metadata already contains all likes, don't do GraphQL requests to obtain them
            yield from (like['node'] for like in likes_edges)
        yield from self._context.graphql_node_list("1cb6ec562846122743b61e492c85999f", {'shortcode': self.shortcode},
                                                   'https://www.instagram.com/p/' + self.shortcode + '/',
                                                   lambda d: d['data']['shortcode_media']['edge_liked_by'])

    def get_location(self) -> Optional[Dict[str, str]]:
        """If the Post has a location, returns a dictionary with fields 'lat' and 'lng'."""
        loc_dict = self._field("location")
        if loc_dict is not None:
            location_json = self._context.get_json("explore/locations/{0}/".format(loc_dict["id"]),
                                                   params={'__a': 1})
            return location_json["location"] if "location" in location_json else location_json['graphql']['location']

    @staticmethod
    def json_encoder(obj) -> Dict[str, Any]:
        """Convert instance of :class:`Post` to a JSON-serializable dictionary."""
        if not isinstance(obj, Post):
            raise TypeError("Object of type {} is not a Post object.".format(obj.__class__.__name__))
        jsondict = {}
        for prop in dir(Post):
            if prop[0].isupper() or prop[0] == '_':
                # skip uppercase and private properties
                continue
            val = obj.__getattribute__(prop)
            if val is True or val is False or isinstance(val, (str, int, float, list)):
                jsondict[prop] = val
            elif isinstance(val, datetime):
                jsondict[prop] = val.isoformat()
        return jsondict


class Profile:
    """
    An Instagram Profile.

    Provides methods for accessing profile properties, as well as :meth:`Profile.get_posts` and for own profile
    :meth:`Profile.get_saved_posts`.

    This class implements == and is hashable.
    """
    def __init__(self, context: InstaloaderContext, profile_name: str):
        """
        Lookup Profile information and create Profile instance.

        :param context: :class:`InstaloaderContext` instance used for queries etc.
        :param identifier: Profile name (string).
        """
        self._context = context

        try:
            metadata = self._context.get_json('{}/'.format(profile_name), params={'__a': 1})
            self._metadata = metadata['graphql'] if 'graphql' in metadata else metadata
        except QueryReturnedNotFoundException:
            raise ProfileNotExistsException('Profile {} does not exist.'.format(profile_name))

    @property
    def userid(self) -> int:
        return int(self._metadata['user']['id'])

    @property
    def username(self) -> str:
        return self._metadata['user']['username']

    def __repr__(self):
        return '<Profile {} ({})>'.format(self.username, self.userid)

    def __eq__(self, o: object) -> bool:
        if isinstance(o, Profile):
            return self.userid == o.userid
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.userid)

    @property
    def is_private(self) -> bool:
        return self._metadata['user']['is_private']

    @property
    def followed_by_viewer(self) -> bool:
        return self._metadata['user']['followed_by_viewer']

    @property
    def mediacount(self) -> int:
        if "media" in self._metadata["user"]:
            # backwards compatibility with old non-graphql structure
            return self._metadata["user"]["media"]["count"]
        return self._metadata["user"]["edge_owner_to_timeline_media"]["count"]

    @property
    def biography(self) -> str:
        return self._metadata['user']['biography']

    @property
    def blocked_by_viewer(self) -> bool:
        return self._metadata['user']['blocked_by_viewer']

    @property
    def follows_viewer(self) -> bool:
        return self._metadata['user']['follows_viewer']

    @property
    def full_name(self) -> str:
        return self._metadata['user']['full_name']

    @property
    def has_blocked_viewer(self) -> bool:
        return self._metadata['user']['has_blocked_viewer']

    @property
    def has_requested_viewer(self) -> bool:
        return self._metadata['user']['has_requested_viewer']

    @property
    def is_verified(self) -> bool:
        return self._metadata['user']['is_verified']

    @property
    def requested_by_viewer(self) -> bool:
        return self._metadata['user']['requested_by_viewer']

    def get_profile_pic_url(self) -> str:
        """Return URL of profile picture"""
        try:
            with self._context.get_anonymous_session() as anonymous_session:
                data = self._context.get_json(path='api/v1/users/{0}/info/'.format(self.userid), params={},
                                              host='i.instagram.com', session=anonymous_session)
            return data["user"]["hd_profile_pic_url_info"]["url"]
        except (InstaloaderException, KeyError) as err:
            self._context.error('{} Unable to fetch high quality profile pic.'.format(err))
            return self._metadata["user"]["profile_pic_url_hd"] if "profile_pic_url_hd" in self._metadata["user"] \
                else self._metadata["user"]["profile_pic_url"]

    def get_posts(self) -> Iterator[Post]:
        """Retrieve all posts from a profile."""
        if 'media' in self._metadata['user']:
            # backwards compatibility with old non-graphql structure
            yield from (Post(self._context, node, owner_profile=self)
                        for node in self._metadata['user']['media']['nodes'])
            has_next_page = self._metadata['user']['media']['page_info']['has_next_page']
            end_cursor = self._metadata['user']['media']['page_info']['end_cursor']
        else:
            yield from (Post(self._context, edge['node'], owner_profile=self)
                        for edge in self._metadata['user']['edge_owner_to_timeline_media']['edges'])
            has_next_page = self._metadata['user']['edge_owner_to_timeline_media']['page_info']['has_next_page']
            end_cursor = self._metadata['user']['edge_owner_to_timeline_media']['page_info']['end_cursor']
        while has_next_page:
            # We do not use self.graphql_node_list() here, because profile_metadata
            # lets us obtain the first 12 nodes 'for free'
            data = self._context.graphql_query("472f257a40c653c64c666ce877d59d2b",
                                               {'id': self.userid,
                                                'first': Instaloader.GRAPHQL_PAGE_LENGTH,
                                                'after': end_cursor},
                                               'https://www.instagram.com/{0}/'.format(self.username))
            media = data['data']['user']['edge_owner_to_timeline_media']
            yield from (Post(self._context, edge['node'], owner_profile=self)
                        for edge in media['edges'])
            has_next_page = media['page_info']['has_next_page']
            end_cursor = media['page_info']['end_cursor']

    def get_saved_posts(self) -> Iterator[Post]:
        """Get Posts that are marked as saved by the user."""

        if self.username != self._context.username:
            raise LoginRequiredException("--login={} required to get that profile's saved posts.".format(self.username))

        data = self._metadata

        while True:
            if "edge_saved_media" in data["user"]:
                is_edge = True
                saved_media = data["user"]["edge_saved_media"]
            else:
                is_edge = False
                saved_media = data["user"]["saved_media"]

            if is_edge:
                yield from (Post(self._context, edge["node"]) for edge in saved_media["edges"])
            else:
                yield from (Post(self._context, node) for node in saved_media["nodes"])

            if not saved_media["page_info"]["has_next_page"]:
                break
            data = self._context.graphql_query("f883d95537fbcd400f466f63d42bd8a1",
                                               {'id': self.userid, 'first': Instaloader.GRAPHQL_PAGE_LENGTH,
                                                'after': saved_media["page_info"]["end_cursor"]})['data']


class Tristate(Enum):
    """Tri-state to encode whether we should save certain information, i.e. videos, captions, comments or geotags.

    :attr:`never`
        Do not save, even if the information is available without any additional request,

    :attr:`no_extra_query`
        Save if and only if available without doing additional queries,

    :attr:`always`
        Save (and query, if neccessary).
    """
    never = 0
    no_extra_query = 1
    always = 2


def _requires_login(func: Callable) -> Callable:
    """Decorator to raise an exception if herewith-decorated function is called without being logged in"""
    @wraps(func)
    def call(instaloader, *args, **kwargs):
        if not instaloader.context.is_logged_in:
            raise LoginRequiredException("--login=USERNAME required.")
        return func(instaloader, *args, **kwargs)
    # pylint:disable=no-member
    call.__doc__ += ":raises LoginRequiredException: If called without being logged in.\n"
    return call


class _PathPattern(str):
    """Class overriding :meth:`str.format` for character substitution in paths for Windows, see issue #84."""

    def format(self, *args: Any, **kwargs: Any) -> str:
        ret = super().format(*args, **kwargs)
        return ret.replace(':', '\ua789') if platform.system() == 'Windows' else ret


class Instaloader:
    GRAPHQL_PAGE_LENGTH = 200

    def __init__(self,
                 sleep: bool = True, quiet: bool = False,
                 user_agent: Optional[str] = None,
                 dirname_pattern: Optional[str] = None,
                 filename_pattern: Optional[str] = None,
                 download_videos: Tristate = Tristate.always,
                 download_video_thumbnails: Tristate = Tristate.always,
                 download_geotags: Tristate = Tristate.no_extra_query,
                 save_captions: Tristate = Tristate.no_extra_query,
                 download_comments: Tristate = Tristate.no_extra_query,
                 save_metadata: Tristate = Tristate.never,
                 max_connection_attempts: int = 3):

        self.context = InstaloaderContext(sleep, quiet, user_agent, max_connection_attempts)

        # configuration parameters
        self.dirname_pattern = _PathPattern(dirname_pattern if dirname_pattern is not None else '{target}')
        if filename_pattern is not None:
            filename_pattern = re.sub(r"({(?:post\.)?date)([:}])", r"\1_utc\2", filename_pattern)
            self.filename_pattern_old = filename_pattern.replace('{date_utc}', '{date_utc:%Y-%m-%d_%H-%M-%S}')
            self.filename_pattern_old = _PathPattern(re.sub(r"(?i)({(?:post\.)?date_utc:[^}]*?)_UTC",
                                                            r"\1", self.filename_pattern_old))
            filename_pattern = re.sub(r"(?i)({(date_utc|post\.date_utc):(?![^}]*UTC[^}]*).*?)}",
                                      r"\1_UTC}", filename_pattern)
            self.filename_pattern = _PathPattern(filename_pattern.replace('{date_utc}',
                                                                          '{date_utc:%Y-%m-%d_%H-%M-%S_UTC}'))
        else:
            self.filename_pattern = _PathPattern('{date_utc:%Y-%m-%d_%H-%M-%S_UTC}')
            self.filename_pattern_old = _PathPattern('{date_utc:%Y-%m-%d_%H-%M-%S}')
        self.download_videos = download_videos
        self.download_video_thumbnails = download_video_thumbnails
        self.download_geotags = download_geotags
        self.save_captions = save_captions
        self.download_comments = download_comments
        self.save_metadata = save_metadata

    @contextmanager
    def anonymous_copy(self):
        """Yield an anonymous, otherwise equally-configured copy of an Instaloader instance; Then copy its error log."""
        new_loader = Instaloader(self.context.sleep, self.context.quiet, self.context.user_agent,
                                 self.dirname_pattern, self.filename_pattern,
                                 self.download_videos,
                                 self.download_video_thumbnails,
                                 self.download_geotags,
                                 self.save_captions, self.download_comments,
                                 self.save_metadata, self.context.max_connection_attempts)
        new_loader.context.previous_queries = self.context.previous_queries
        yield new_loader
        self.context.error_log.extend(new_loader.context.error_log)
        new_loader.context.error_log = []  # avoid double-printing of errors
        self.context.previous_queries = new_loader.context.previous_queries
        new_loader.close()

    def close(self):
        self.context.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get_username_by_id(self, profile_id: int) -> str:
        """To get the current username of a profile, given its unique ID, this function can be used."""
        data = self.context.graphql_query("472f257a40c653c64c666ce877d59d2b",
                                          {'id': str(profile_id), 'first': 1})['data']['user']
        if data:
            data = data["edge_owner_to_timeline_media"]
        else:
            raise ProfileNotExistsException("No profile found, the user may have blocked you (ID: " +
                                            str(profile_id) + ").")
        if not data['edges']:
            if data['count'] == 0:
                raise ProfileHasNoPicsException("Profile with ID {0}: no pics found.".format(str(profile_id)))
            else:
                raise LoginRequiredException("Login required to determine username (ID: " + str(profile_id) + ").")
        else:
            return Post.from_mediaid(self.context, int(data['edges'][0]["node"]["id"])).owner_username

    def get_id_by_username(self, profile: str) -> int:
        """Each Instagram profile has its own unique ID which stays unmodified even if a user changes
        his/her username. To get said ID, given the profile's name, you may call this function."""
        return Profile(self.context, profile).userid

    @_requires_login
    def get_followers(self, profile: str) -> Iterator[Dict[str, Any]]:
        """
        Retrieve list of followers of given profile.
        To use this, one needs to be logged in and private profiles has to be followed,
        otherwise this returns an empty list.

        :param profile: Name of profile to lookup followers.
        """
        yield from self.context.graphql_node_list("37479f2b8209594dde7facb0d904896a",
                                                  {'id': str(self.get_id_by_username(profile))},
                                                  'https://www.instagram.com/' + profile + '/',
                                                  lambda d: d['data']['user']['edge_followed_by'])

    @_requires_login
    def get_followees(self, profile: str) -> Iterator[Dict[str, Any]]:
        """
        Retrieve list of followees (followings) of given profile.
        To use this, one needs to be logged in and private profiles has to be followed,
        otherwise this returns an empty list.

        :param profile: Name of profile to lookup followers.
        """
        yield from self.context.graphql_node_list("58712303d941c6855d4e888c5f0cd22f",
                                                  {'id': str(self.get_id_by_username(profile))},
                                                  'https://www.instagram.com/' + profile + '/',
                                                  lambda d: d['data']['user']['edge_follow'])

    def download_pic(self, filename: str, url: str, mtime: datetime,
                     filename_alt: Optional[str] = None, filename_suffix: Optional[str] = None) -> bool:
        """Downloads and saves picture with given url under given directory with given timestamp.
        Returns true, if file was actually downloaded, i.e. updated."""
        urlmatch = re.search('\\.[a-z0-9]*\\?', url)
        file_extension = url[-3:] if urlmatch is None else urlmatch.group(0)[1:-1]
        if filename_suffix is not None:
            filename += '_' + filename_suffix
            if filename_alt is not None:
                filename_alt += '_' + filename_suffix
        filename += '.' + file_extension
        if os.path.isfile(filename):
            self.context.log(filename + ' exists', end=' ', flush=True)
            return False
        if filename_alt is not None:
            filename_alt += '.' + file_extension
            if os.path.isfile(filename_alt):
                self.context.log(filename_alt + 'exists', end=' ', flush=True)
                return False
        self.context.get_and_write_raw(url, filename)
        os.utime(filename, (datetime.now().timestamp(), mtime.timestamp()))
        return True

    def save_metadata_json(self, filename: str, post: Post) -> None:
        """Saves metadata JSON file of a :class:`Post`."""
        filename += '.json'
        with open(filename, 'w') as fp:
            json.dump(post, fp=fp, indent=4, default=Post.json_encoder)
        self.context.log('json', end=' ', flush=True)

    def update_comments(self, filename: str, post: Post, filename_alt: Optional[str] = None) -> None:
        try:
            filename_current = filename + '_comments.json'
            comments = json.load(open(filename_current))
        except FileNotFoundError:
            try:
                filename_current = filename_alt + '_comments.json'
                comments = json.load(open(filename_current))
            except (FileNotFoundError, TypeError):
                filename_current = filename + '_comments.json'
                comments = list()
        comments.extend(post.get_comments())
        if comments:
            with open(filename_current, 'w') as file:
                comments_list = sorted(sorted(list(comments), key=lambda t: t['id']),
                                       key=lambda t: t['created_at'], reverse=True)
                unique_comments_list = [comments_list[0]]
                #for comment in comments_list:
                #    if unique_comments_list[-1]['id'] != comment['id']:
                #        unique_comments_list.append(comment)
                #file.write(json.dumps(unique_comments_list, indent=4))
                for x, y in zip(comments_list[:-1], comments_list[1:]):
                    if x['id'] != y['id']:
                        unique_comments_list.append(y)
                file.write(json.dumps(unique_comments_list, indent=4))
            os.rename(filename_current, filename + '_comments.json')
            self.context.log('comments', end=' ', flush=True)

    def save_caption(self, filename: str, mtime: datetime, caption: str, filename_alt: Optional[str] = None) -> None:
        """Updates picture caption"""
        filename += '.txt'
        if filename_alt is not None:
            filename_alt += '.txt'
        pcaption = caption.replace('\n', ' ').strip()
        caption = caption.encode("UTF-8")
        pcaption = '[' + ((pcaption[:29] + u"\u2026") if len(pcaption) > 31 else pcaption) + ']'
        with suppress(FileNotFoundError):
            try:
                with open(filename, 'rb') as file:
                    file_caption = file.read()
            except FileNotFoundError:
                if filename_alt is not None:
                    with open(filename_alt, 'rb') as file:
                        file_caption = file.read()
            if file_caption.replace(b'\r\n', b'\n') == caption.replace(b'\r\n', b'\n'):
                try:
                    self.context.log(pcaption + ' unchanged', end=' ', flush=True)
                except UnicodeEncodeError:
                    self.context.log('txt unchanged', end=' ', flush=True)
                return None
            else:
                def get_filename(file, index):
                    return file if index == 0 else (file[:-4] + '_old_' +
                                                    (str(0) if index < 10 else str()) + str(index) + file[-4:])

                i = 0
                file_exists_list = []
                while True:
                    file_exists_list.append(1 if os.path.isfile(get_filename(filename, i)) else 0)
                    if not file_exists_list[i] and filename_alt is not None:
                        file_exists_list[i] = 2 if os.path.isfile(get_filename(filename_alt, i)) else 0
                    if not file_exists_list[i]:
                        break
                    i = i + 1
                for index in range(i, 0, -1):
                    os.rename(get_filename(filename if file_exists_list[index - 1] % 2 else filename_alt, index - 1),
                              get_filename(filename, index))
                try:
                    self.context.log(pcaption + ' updated', end=' ', flush=True)
                except UnicodeEncodeError:
                    self.context.log('txt updated', end=' ', flush=True)
        try:
            self.context.log(pcaption, end=' ', flush=True)
        except UnicodeEncodeError:
            self.context.log('txt', end=' ', flush=True)
        with open(filename, 'wb') as text_file:
            shutil.copyfileobj(BytesIO(caption), text_file)
        os.utime(filename, (datetime.now().timestamp(), mtime.timestamp()))

    def save_location(self, filename: str, location_json: Dict[str, str], mtime: datetime) -> None:
        filename += '_location.txt'
        location_string = (location_json["name"] + "\n" +
                           "https://maps.google.com/maps?q={0},{1}&ll={0},{1}\n".format(location_json["lat"],
                                                                                        location_json["lng"]))
        with open(filename, 'wb') as text_file:
            shutil.copyfileobj(BytesIO(location_string.encode()), text_file)
        os.utime(filename, (datetime.now().timestamp(), mtime.timestamp()))
        self.context.log('geo', end=' ', flush=True)

    def download_profilepic(self, profile: Profile) -> None:
        """Downloads and saves profile pic."""

        def _epoch_to_string(epoch: datetime) -> str:
            return epoch.strftime('%Y-%m-%d_%H-%M-%S')

        profile_pic_url = profile.get_profile_pic_url()
        with self.context.get_anonymous_session() as anonymous_session:
            date_object = datetime.strptime(anonymous_session.head(profile_pic_url).headers["Last-Modified"],
                                            '%a, %d %b %Y %H:%M:%S GMT')
        if ((format_string_contains_key(self.dirname_pattern, 'profile') or
             format_string_contains_key(self.dirname_pattern, 'target'))):
            filename = '{0}/{1}_UTC_profile_pic.{2}'.format(self.dirname_pattern.format(profile=profile.username.lower(),
                                                                                        target=profile.username.lower()),
                                                            _epoch_to_string(date_object), profile_pic_url[-3:])
        else:
            filename = '{0}/{1}_{2}_UTC_profile_pic.{3}'.format(self.dirname_pattern.format(), profile.username.lower(),
                                                                _epoch_to_string(date_object), profile_pic_url[-3:])
        if os.path.isfile(filename):
            self.context.log(filename + ' already exists')
            return None
        self.context.get_and_write_raw(profile_pic_url, filename)
        os.utime(filename, (datetime.now().timestamp(), date_object.timestamp()))
        self.context.log('') # log output of _get_and_write_raw() does not produce \n

    @_requires_login
    def save_session_to_file(self, filename: Optional[str] = None) -> None:
        """Saves internally stored :class:`requests.Session` object."""
        if filename is None:
            filename = get_default_session_filename(self.context.username)
        dirname = os.path.dirname(filename)
        if dirname != '' and not os.path.exists(dirname):
            os.makedirs(dirname)
            os.chmod(dirname, 0o700)
        with open(filename, 'wb') as sessionfile:
            os.chmod(filename, 0o600)
            self.context.save_session_to_file(sessionfile)
            self.context.log("Saved session to %s." % filename)

    def load_session_from_file(self, username: str, filename: Optional[str] = None) -> None:
        """Internally stores :class:`requests.Session` object loaded from file.

        If filename is None, the file with the default session path is loaded.

        :raises FileNotFoundError: If the file does not exist.
        """
        if filename is None:
            filename = get_default_session_filename(username)
        with open(filename, 'rb') as sessionfile:
            self.context.load_session_from_file(username, sessionfile)
            self.context.log("Loaded session from %s." % filename)

    def test_login(self) -> Optional[str]:
        """Returns the Instagram username to which given :class:`requests.Session` object belongs, or None."""
        return self.context.test_login()

    def login(self, user: str, passwd: str) -> None:
        """Log in to instagram with given username and password and internally store session object"""
        self.context.login(user, passwd)

    def download_post(self, post: Post, target: str) -> bool:
        """
        Download everything associated with one instagram post node, i.e. picture, caption and video.

        :param post: Post to download.
        :param target: Target name, i.e. profile name, #hashtag, :feed; for filename.
        :return: True if something was downloaded, False otherwise, i.e. file was already there
        """

        # Format dirname and filename. post.owner_username might do an additional request, so only access it, if
        # {profile} is part of the dirname pattern or filename pattern.
        needs_profilename = (format_string_contains_key(self.dirname_pattern, 'profile') or
                             format_string_contains_key(self.filename_pattern, 'profile'))
        profilename = post.owner_username if needs_profilename else None
        dirname = self.dirname_pattern.format(profile=profilename, target=target.lower())
        filename = dirname + '/' + self.filename_pattern.format(profile=profilename, target=target.lower(),
                                                                date_utc=post.date_utc,
                                                                shortcode=post.shortcode,
                                                                post=post)
        filename_old = dirname + '/' + self.filename_pattern_old.replace("{post.date_utc", "{date_utc") \
                                                                .format(profile=profilename, target=target.lower(),
                                                                        date_utc=post.date_local,
                                                                        shortcode=post.shortcode,
                                                                        post=post)
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        # Download the image(s) / video thumbnail and videos within sidecars if desired
        downloaded = False
        if post.typename == 'GraphSidecar':
            edge_number = 1
            for edge in post.get_sidecar_edges():
                # Download picture or video thumbnail
                if not edge['node']['is_video'] or self.download_video_thumbnails is Tristate.always:
                    downloaded |= self.download_pic(filename=filename,
                                                    filename_alt=filename_old,
                                                    url=edge['node']['display_url'],
                                                    mtime=post.date_local,
                                                    filename_suffix=str(edge_number))
                # Additionally download video if available and desired
                if edge['node']['is_video'] and self.download_videos is Tristate.always:
                    downloaded |= self.download_pic(filename=filename,
                                                    filename_alt=filename_old,
                                                    url=edge['node']['video_url'],
                                                    mtime=post.date_local,
                                                    filename_suffix=str(edge_number))
                edge_number += 1
        elif post.typename == 'GraphImage':
            downloaded = self.download_pic(filename=filename, filename_alt=filename_old,
                                           url=post.url, mtime=post.date_local)
        elif post.typename == 'GraphVideo':
            if self.download_video_thumbnails is Tristate.always:
                downloaded = self.download_pic(filename=filename, filename_alt=filename_old,
                                               url=post.url, mtime=post.date_local)
        else:
            self.context.error("Warning: {0} has unknown typename: {1}".format(post, post.typename))

        # Save caption if desired
        if self.save_captions is not Tristate.never:
            if post.caption:
                self.save_caption(filename=filename, filename_alt=filename_old,
                                  mtime=post.date_local, caption=post.caption)
            else:
                self.context.log("<no caption>", end=' ', flush=True)

        # Download video if desired
        if post.is_video and self.download_videos is Tristate.always:
            downloaded |= self.download_pic(filename=filename, filename_alt=filename_old,
                                            url=post.video_url, mtime=post.date_local)

        # Download geotags if desired
        if self.download_geotags is Tristate.always:
            location = post.get_location()
            if location:
                self.save_location(filename, location, post.date_local)

        # Update comments if desired
        if self.download_comments is Tristate.always:
            self.update_comments(filename=filename, filename_alt=filename_old, post=post)

        # Save metadata as JSON if desired.  It might require an extra query, depending on which information has been
        # already obtained.  Regarding Tristate interpretation, we always assume that it requires an extra query.
        if self.save_metadata is Tristate.always:
            self.save_metadata_json(filename, post)

        self.context.log()
        return downloaded

    @_requires_login
    def get_stories(self, userids: Optional[List[int]] = None) -> Iterator[Dict[str, Any]]:
        """Get available stories from followees or all stories of users whose ID are given.
        Does not mark stories as seen.
        To use this, one needs to be logged in

        :param userids: List of user IDs to be processed in terms of downloading their stories, or None.
        """

        if userids is None:
            data = self.context.graphql_query("d15efd8c0c5b23f0ef71f18bf363c704",
                                              {"only_stories": True})["data"]["user"]
            if data is None:
                raise BadResponseException('Bad stories reel JSON.')
            userids = list(edge["node"]["id"] for edge in data["feed_reels_tray"]["edge_reels_tray_to_reel"]["edges"])

        stories = self.context.graphql_query("bf41e22b1c4ba4c9f31b844ebb7d9056",
                                             {"reel_ids": userids, "precomposed_overlay": False})["data"]

        for media in stories["reels_media"]:
            yield media

    @_requires_login
    def download_stories(self,
                         userids: Optional[List[int]] = None,
                         fast_update: bool = False,
                         filename_target: str = ':stories') -> None:
        """
        Download available stories from user followees or all stories of users whose ID are given.
        Does not mark stories as seen.
        To use this, one needs to be logged in

        :param userids: List of user IDs to be processed in terms of downloading their stories
        :param fast_update: If true, abort when first already-downloaded picture is encountered
        :param filename_target: Replacement for {target} in dirname_pattern and filename_pattern
        """

        if not userids:
            self.context.log("Retrieving all visible stories...")

        if format_string_contains_key(self.filename_pattern, 'post'):
            raise InvalidArgumentException("The \"post\" keyword is not supported in the filename pattern when "
                                           "downloading stories.")

        for user_stories in self.get_stories(userids):
            if "items" not in user_stories:
                raise BadResponseException('Bad reel media JSON.')
            name = user_stories["user"]["username"].lower()
            self.context.log("Retrieving stories from profile {}.".format(name))
            totalcount = len(user_stories["items"])
            count = 1
            for item in user_stories["items"]:
                self.context.log("[%3i/%3i] " % (count, totalcount), end="", flush=True)
                count += 1
                with self.context.error_catcher('Download story from user {}'.format(name)):
                    downloaded = self.download_story(item, filename_target, name)
                    if fast_update and not downloaded:
                        break

    def download_story(self, item: Dict[str, Any], target: str, profile: str) -> bool:
        """Download one user story.

        :param item: Story item, as in story['items'] for story in :meth:`get_stories`
        :param target: Replacement for {target} in dirname_pattern and filename_pattern
        :param profile: Owner profile name
        :return: True if something was downloaded, False otherwise, i.e. file was already there
        """

        shortcode = mediaid_to_shortcode(int(item["id"]))
        date_local = datetime.fromtimestamp(item["taken_at_timestamp"])
        date_utc = datetime.utcfromtimestamp(item["taken_at_timestamp"])
        dirname = self.dirname_pattern.format(profile=profile, target=target)
        filename = dirname + '/' + self.filename_pattern.format(profile=profile, target=target,
                                                                date_utc=date_utc,
                                                                shortcode=shortcode)
        filename_old = dirname + '/' + self.filename_pattern_old.format(profile=profile, target=target,
                                                                        date_utc=date_local,
                                                                        shortcode=shortcode)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        downloaded = False
        if not item["is_video"] or self.download_video_thumbnails is Tristate.always:
            url = item["display_resources"][-1]["src"]
            downloaded = self.download_pic(filename=filename,
                                           filename_alt=filename_old,
                                           url=url,
                                           mtime=date_local)
        if item["is_video"] and self.download_videos is Tristate.always:
            downloaded |= self.download_pic(filename=filename,
                                            filename_alt=filename_old,
                                            url=item["video_resources"][-1]["src"],
                                            mtime=date_local)
        self.context.log()
        return downloaded

    @_requires_login
    def get_feed_posts(self) -> Iterator[Post]:
        """Get Posts of the user's feed."""

        data = self.context.graphql_query("d6f4427fbe92d846298cf93df0b937d3", {})["data"]

        while True:
            feed = data["user"]["edge_web_feed_timeline"]
            yield from (Post(self.context, edge["node"]) for edge in feed["edges"]
                        if not edge["node"]["__typename"] == "GraphSuggestedUserFeedUnit")
            if not feed["page_info"]["has_next_page"]:
                break
            data = self.context.graphql_query("d6f4427fbe92d846298cf93df0b937d3",
                                              {'fetch_media_item_count': 12,
                                               'fetch_media_item_cursor': feed["page_info"]["end_cursor"],
                                               'fetch_comment_count': 4,
                                               'fetch_like': 10,
                                               'has_stories': False})["data"]

    @_requires_login
    def download_feed_posts(self, max_count: int = None, fast_update: bool = False,
                            filter_func: Optional[Callable[[Post], bool]] = None) -> None:
        """
        Download pictures from the user's feed.

        Example to download up to the 20 pics the user last liked::

            loader = Instaloader()
            loader.load_session_from_file('USER')
            loader.download_feed_posts(max_count=20, fast_update=True,
                                       filter_func=lambda post: post.viewer_has_liked)

        :param max_count: Maximum count of pictures to download
        :param fast_update: If true, abort when first already-downloaded picture is encountered
        :param filter_func: function(post), which returns True if given picture should be downloaded
        """
        self.context.log("Retrieving pictures from your feed...")
        count = 1
        for post in self.get_feed_posts():
            if max_count is not None and count > max_count:
                break
            name = post.owner_username
            if filter_func is not None and not filter_func(post):
                self.context.log("<pic by %s skipped>" % name, flush=True)
                continue
            self.context.log("[%3i] %s " % (count, name), end="", flush=True)
            count += 1
            with self.context.error_catcher('Download feed'):
                downloaded = self.download_post(post, target=':feed')
                if fast_update and not downloaded:
                    break

    @_requires_login
    def download_saved_posts(self, max_count: int = None, fast_update: bool = False,
                             filter_func: Optional[Callable[[Post], bool]] = None) -> None:
        """Download user's saved pictures.

        :param max_count: Maximum count of pictures to download
        :param fast_update: If true, abort when first already-downloaded picture is encountered
        :param filter_func: function(post), which returns True if given picture should be downloaded
        """
        self.context.log("Retrieving saved posts...")
        count = 1
        for post in Profile(self.context, self.context.username).get_saved_posts():
            if max_count is not None and count > max_count:
                break
            name = post.owner_username
            if filter_func is not None and not filter_func(post):
                self.context.log("<pic by {} skipped".format(name), flush=True)
                continue
            self.context.log("[{:>3}] {} ".format(count, name), end=str(), flush=True)
            count += 1
            with self.context.error_catcher('Download saved posts'):
                downloaded = self.download_post(post, target=':saved')
                if fast_update and not downloaded:
                    break

    @_requires_login
    def get_explore_posts(self) -> Iterator[Post]:
        """Get Posts which are worthy of exploring suggested by Instagram."""
        yield from (Post(self.context, node)
                    for node in self.context.graphql_node_list("df0dcc250c2b18d9fd27c5581ef33c7c",
                                                               {}, 'https://www.instagram.com/explore/',
                                                               lambda d: d['data']['user']['edge_web_discover_media']))

    def get_hashtag_posts(self, hashtag: str) -> Iterator[Post]:
        """Get Posts associated with a #hashtag."""
        yield from (Post(self.context, node)
                    for node in self.context.graphql_node_list("298b92c8d7cad703f7565aa892ede943",
                                                               {'tag_name': hashtag},
                                                               'https://www.instagram.com/explore/tags/{0}/'
                                                               .format(hashtag),
                                                               lambda d: d['data']['hashtag']['edge_hashtag_to_media']))

    def download_hashtag(self, hashtag: str,
                         max_count: Optional[int] = None,
                         filter_func: Optional[Callable[[Post], bool]] = None,
                         fast_update: bool = False) -> None:
        """Download pictures of one hashtag.

        To download the last 30 pictures with hashtag #cat, do::

            loader = Instaloader()
            loader.download_hashtag('cat', max_count=30)

        :param hashtag: Hashtag to download, without leading '#'
        :param max_count: Maximum count of pictures to download
        :param filter_func: function(post), which returns True if given picture should be downloaded
        :param fast_update: If true, abort when first already-downloaded picture is encountered
        """
        hashtag = hashtag.lower()
        self.context.log("Retrieving pictures with hashtag {}...".format(hashtag))
        count = 1
        for post in self.get_hashtag_posts(hashtag):
            if max_count is not None and count > max_count:
                break
            self.context.log('[{0:3d}] #{1} '.format(count, hashtag), end='', flush=True)
            if filter_func is not None and not filter_func(post):
                self.context.log('<skipped>')
                continue
            count += 1
            with self.context.error_catcher('Download hashtag #{}'.format(hashtag)):
                downloaded = self.download_post(post, target='#' + hashtag)
                if fast_update and not downloaded:
                    break

    def check_profile_id(self, profile_name: str, profile: Optional[Profile] = None) -> str:
        """
        Consult locally stored ID of profile with given name, check whether ID matches and whether name
        has changed and return current name of the profile, and store ID of profile.

        :param profile_name: Profile name
        :param profile: The :class:`Profile`, or None if the profile was not found
        :return: current profile name, profile id
        """
        profile_exists = profile is not None
        if ((format_string_contains_key(self.dirname_pattern, 'profile') or
             format_string_contains_key(self.dirname_pattern, 'target'))):
            id_filename = '{0}/id'.format(self.dirname_pattern.format(profile=profile_name.lower(),
                                                                      target=profile_name.lower()))
        else:
            id_filename = '{0}/{1}_id'.format(self.dirname_pattern.format(), profile_name.lower())
        try:
            with open(id_filename, 'rb') as id_file:
                profile_id = int(id_file.read())
            if (not profile_exists) or \
                    (profile_id != profile.userid):
                if profile_exists:
                    self.context.log("Profile {0} does not match the stored unique ID {1}.".format(profile_name,
                                                                                                   profile_id))
                else:
                    self.context.log("Trying to find profile {0} using its unique ID {1}.".format(profile_name,
                                                                                                  profile_id))
                newname = self.get_username_by_id(profile_id)
                self.context.log("Profile {0} has changed its name to {1}.".format(profile_name, newname))
                if ((format_string_contains_key(self.dirname_pattern, 'profile') or
                     format_string_contains_key(self.dirname_pattern, 'target'))):
                    os.rename(self.dirname_pattern.format(profile=profile_name.lower(),
                                                          target=profile_name.lower()),
                              self.dirname_pattern.format(profile=newname.lower(),
                                                          target=newname.lower()))
                else:
                    os.rename('{0}/{1}_id'.format(self.dirname_pattern.format(), profile_name.lower()),
                              '{0}/{1}_id'.format(self.dirname_pattern.format(), newname.lower()))
                return newname
            return profile_name
        except FileNotFoundError:
            pass
        if profile_exists:
            os.makedirs(self.dirname_pattern.format(profile=profile_name.lower(),
                                                    target=profile_name.lower()), exist_ok=True)
            with open(id_filename, 'w') as text_file:
                text_file.write(str(profile.userid) + "\n")
                self.context.log("Stored ID {0} for profile {1}.".format(profile.userid, profile_name))
            return profile_name
        raise ProfileNotExistsException("Profile {0} does not exist.".format(profile_name))

    def download_profile(self, profile_name: str,
                         profile_pic: bool = True, profile_pic_only: bool = False,
                         fast_update: bool = False,
                         download_stories: bool = False, download_stories_only: bool = False,
                         filter_func: Optional[Callable[[Post], bool]] = None) -> None:
        """Download one profile"""
        profile_name = profile_name.lower()

        # Get profile main page json
        profile = None
        with suppress(ProfileNotExistsException):
            # ProfileNotExistsException is raised again later in check_profile_id() when we search the profile, so we
            # must suppress it here.
            profile = Profile(self.context, profile_name)

        # check if profile does exist or name has changed since last download
        # and update name and json data if necessary
        name_updated = self.check_profile_id(profile_name, profile)
        if name_updated != profile_name:
            profile_name = name_updated
            profile = Profile(self.context, profile_name)

        if self.context.is_logged_in and profile.has_blocked_viewer and not profile.is_private:
            # raising ProfileNotExistsException invokes "trying again anonymously" logic
            raise ProfileNotExistsException("Profile {} has blocked you".format(profile_name))

        # Download profile picture
        if profile_pic or profile_pic_only:
            with self.context.error_catcher('Download profile picture of {}'.format(profile_name)):
                self.download_profilepic(profile)
        if profile_pic_only:
            return

        # Catch some errors
        if profile.is_private:
            if not self.context.is_logged_in:
                raise LoginRequiredException("profile %s requires login" % profile_name)
            if not profile.followed_by_viewer and \
                    self.context.username != profile.username:
                raise PrivateProfileNotFollowedException("Profile %s: private but not followed." % profile_name)
        else:
            if self.context.is_logged_in and not (download_stories or download_stories_only):
                self.context.log("profile %s could also be downloaded anonymously." % profile_name)

        # Download stories, if requested
        if download_stories or download_stories_only:
            with self.context.error_catcher("Download stories of {}".format(profile_name)):
                self.download_stories(userids=[profile.userid], filename_target=profile_name, fast_update=fast_update)
        if download_stories_only:
            return

        # Iterate over pictures and download them
        self.context.log("Retrieving posts from profile {}.".format(profile_name))
        totalcount = profile.mediacount
        count = 1
        for post in profile.get_posts():
            self.context.log("[%3i/%3i] " % (count, totalcount), end="", flush=True)
            count += 1
            if filter_func is not None and not filter_func(post):
                self.context.log('<skipped>')
                continue
            with self.context.error_catcher('Download profile {}'.format(profile_name)):
                downloaded = self.download_post(post, target=profile_name)
                if fast_update and not downloaded:
                    break

    def interactive_login(self, username: str) -> None:
        """Logs in and internally stores session, asking user for password interactively.

        :raises LoginRequiredException: when in quiet mode."""
        if self.context.quiet:
            raise LoginRequiredException("Quiet mode requires given password or valid session file.")
        password = None
        while password is None:
            password = getpass.getpass(prompt="Enter Instagram password for %s: " % username)
            try:
                self.login(username, password)
            except BadCredentialsException as err:
                print(err, file=sys.stderr)
                password = None

    def main(self, targetlist: List[str], username: Optional[str] = None, password: Optional[str] = None,
             sessionfile: Optional[str] = None, max_count: Optional[int] = None,
             profile_pic: bool = True, profile_pic_only: bool = False,
             fast_update: bool = False,
             stories: bool = False, stories_only: bool = False,
             filter_str: Optional[str] = None) -> None:
        """Download set of profiles, hashtags etc. and handle logging in and session files if desired."""
        # Parse and generate filter function
        if filter_str is not None:
            filter_func = filterstr_to_filterfunc(filter_str, username is not None)
            self.context.log('Only download posts with property "{}".'.format(filter_str))
        else:
            filter_func = None
        # Login, if desired
        if username is not None:
            try:
                self.load_session_from_file(username, sessionfile)
            except FileNotFoundError as err:
                if sessionfile is not None:
                    print(err, file=sys.stderr)
                self.context.log("Session file does not exist yet - Logging in.")
            if not self.context.is_logged_in or username != self.test_login():
                if password is not None:
                    self.login(username, password)
                else:
                    self.interactive_login(username)
            self.context.log("Logged in as %s." % username)
        # Try block for KeyboardInterrupt (save session on ^C)
        profiles = set()
        try:
            # Generate set of profiles, already downloading non-profile targets
            for target in targetlist:
                # strip '/' characters to be more shell-autocompletion-friendly
                target = target.rstrip('/')
                with self.context.error_catcher(target):
                    if target[0] == '@':
                        self.context.log("Retrieving followees of %s..." % target[1:])
                        profiles.update([followee['username'] for followee in self.get_followees(target[1:])])
                    elif target[0] == '#':
                        self.download_hashtag(hashtag=target[1:], max_count=max_count, fast_update=fast_update,
                                              filter_func=filter_func)
                    elif target == ":feed":
                        self.download_feed_posts(fast_update=fast_update, max_count=max_count,
                                                 filter_func=filter_func)
                    elif target == ":stories":
                        self.download_stories(fast_update=fast_update)
                    elif target == ":saved":
                        self.download_saved_posts(fast_update=fast_update, max_count=max_count,
                                                  filter_func=filter_func)
                    else:
                        profiles.add(target)
            if len(profiles) > 1:
                self.context.log("Downloading {} profiles: {}".format(len(profiles), ','.join(profiles)))
            # Iterate through profiles list and download them
            for target in profiles:
                with self.context.error_catcher(target):
                    try:
                        self.download_profile(target, profile_pic, profile_pic_only, fast_update, stories, stories_only,
                                              filter_func=filter_func)
                    except ProfileNotExistsException as err:
                        if not self.context.is_logged_in:
                            self.context.log(err)
                            self.context.log("Trying again anonymously, helps in case you are just blocked.")
                            with self.anonymous_copy() as anonymous_loader:
                                with self.context.error_catcher():
                                    anonymous_loader.download_profile(target, profile_pic, profile_pic_only,
                                                                      fast_update, filter_func=filter_func)
                        else:
                            raise err
        except KeyboardInterrupt:
            print("\nInterrupted by user.", file=sys.stderr)
        # Save session if it is useful
        if self.context.is_logged_in:
            self.save_session_to_file(sessionfile)
        # User might be confused if Instaloader does nothing
        if not targetlist:
            if self.context.is_logged_in:
                # Instaloader did at least save a session file
                self.context.log("No targets were specified, thus nothing has been downloaded.")
            else:
                # Instloader did not do anything
                self.context.log("usage:"+USAGE_STRING)


def main():
    parser = ArgumentParser(description=__doc__, add_help=False, usage=USAGE_STRING,
                            epilog="Report issues at https://github.com/instaloader/instaloader/issues. "
                                   "The complete documentation can be found at "
                                   "https://instaloader.github.io/.")

    g_what = parser.add_argument_group('What to Download',
                                       'Specify a list of profiles or #hashtags. For each of these, Instaloader '
                                       'creates a folder and '
                                       'downloads all posts along with the pictures\'s '
                                       'captions and the current profile picture. '
                                       'If an already-downloaded profile has been renamed, Instaloader automatically '
                                       'finds it by its unique ID and renames the folder likewise.')
    g_what.add_argument('profile', nargs='*', metavar='profile|#hashtag',
                        help='Name of profile or #hashtag to download. '
                             'Alternatively, if --login is given: @<profile> to download all followees of '
                             '<profile>; the special targets '
                             ':feed to download pictures from your feed; '
                             ':stories to download the stories of your followees; or '
                             ':saved to download the posts marked as saved.')
    g_what.add_argument('-P', '--profile-pic-only', action='store_true',
                        help='Only download profile picture.')
    g_what.add_argument('--no-profile-pic', action='store_true',
                        help='Do not download profile picture.')
    g_what.add_argument('-V', '--no-videos', action='store_true',
                        help='Do not download videos.')
    g_what.add_argument('--no-video-thumbnails', action='store_true',
                        help='Do not download thumbnails of videos.')
    g_what.add_argument('-G', '--geotags', action='store_true',
                        help='Download geotags when available. Geotags are stored as a '
                             'text file with the location\'s name and a Google Maps link. '
                             'This requires an additional request to the Instagram '
                             'server for each picture, which is why it is disabled by default.')
    g_what.add_argument('--no-geotags', action='store_true',
                        help='Do not store geotags, even if they can be obtained without any additional request.')
    g_what.add_argument('-C', '--comments', action='store_true',
                        help='Download and update comments for each post. '
                             'This requires an additional request to the Instagram '
                             'server for each post, which is why it is disabled by default.')
    g_what.add_argument('--no-captions', action='store_true',
                        help='Do not store media captions, although no additional request is needed to obtain them.')
    g_what.add_argument('--metadata-json', action='store_true',
                        help='Create a JSON file containing the metadata of each post. This does not include comments '
                             'nor geotags.')
    g_what.add_argument('-s', '--stories', action='store_true',
                        help='Also download stories of each profile that is downloaded. Requires --login.')
    g_what.add_argument('--stories-only', action='store_true',
                        help='Rather than downloading regular posts of each specified profile, only download '
                             'stories. Requires --login. Does not imply --no-profile-pic.')
    g_what.add_argument('--only-if', metavar='filter',
                        help='Expression that, if given, must evaluate to True for each post to be downloaded. Must be '
                             'a syntactically valid python expression. Variables are evaluated to '
                             'instaloader.Post attributes. Example: --only-if=viewer_has_liked.')

    g_stop = parser.add_argument_group('When to Stop Downloading',
                                       'If none of these options are given, Instaloader goes through all pictures '
                                       'matching the specified targets.')
    g_stop.add_argument('-F', '--fast-update', action='store_true',
                        help='For each target, stop when encountering the first already-downloaded picture. This '
                             'flag is recommended when you use Instaloader to update your personal Instagram archive.')
    g_stop.add_argument('-c', '--count',
                        help='Do not attempt to download more than COUNT posts. '
                             'Applies only to #hashtag and :feed.')

    g_login = parser.add_argument_group('Login (Download Private Profiles)',
                                        'Instaloader can login to Instagram. This allows downloading private profiles. '
                                        'To login, pass the --login option. Your session cookie (not your password!) '
                                        'will be saved to a local file to be reused next time you want Instaloader '
                                        'to login.')
    g_login.add_argument('-l', '--login', metavar='YOUR-USERNAME',
                         help='Login name (profile name) for your Instagram account.')
    g_login.add_argument('-f', '--sessionfile',
                         help='Path for loading and storing session key file. '
                              'Defaults to ' + get_default_session_filename("<login_name>"))
    g_login.add_argument('-p', '--password', metavar='YOUR-PASSWORD',
                         help='Password for your Instagram account. Without this option, '
                              'you\'ll be prompted for your password interactively if '
                              'there is not yet a valid session file.')

    g_how = parser.add_argument_group('How to Download')
    g_how.add_argument('--dirname-pattern',
                       help='Name of directory where to store posts. {profile} is replaced by the profile name, '
                            '{target} is replaced by the target you specified, i.e. either :feed, #hashtag or the '
                            'profile name. Defaults to \'{target}\'.')
    g_how.add_argument('--filename-pattern',
                       help='Prefix of filenames. Posts are stored in the directory whose pattern is given with '
                            '--dirname-pattern. {profile} is replaced by the profile name, '
                            '{target} is replaced by the target you specified, i.e. either :feed, #hashtag or the '
                            'profile name. Also, the fields {date} and {shortcode} can be specified. In case of not '
                            'downloading stories, the attributes of the Post class can be used in addition, e.g. '
                            '{post.owner_id} or {post.mediaid}. Defaults to \'{date:%%Y-%%m-%%d_%%H-%%M-%%S}\'.')
    g_how.add_argument('--user-agent',
                       help='User Agent to use for HTTP requests. Defaults to \'{}\'.'.format(default_user_agent()))
    g_how.add_argument('-S', '--no-sleep', action='store_true', help=SUPPRESS)
    g_how.add_argument('--max-connection-attempts', metavar='N', type=int, default=3,
                       help='Maximum number of connection attempts until a request is aborted. Defaults to 3. If a '
                            'connection fails, it can be manually skipped by hitting CTRL+C. Set this to 0 to retry '
                            'infinitely.')

    g_misc = parser.add_argument_group('Miscellaneous Options')
    g_misc.add_argument('-q', '--quiet', action='store_true',
                        help='Disable user interaction, i.e. do not print messages (except errors) and fail '
                             'if login credentials are needed but not given. This makes Instaloader suitable as a '
                             'cron job.')
    g_misc.add_argument('-h', '--help', action='help', help='Show this help message and exit.')
    g_misc.add_argument('--version', action='version', help='Show version number and exit.',
                        version=__version__)

    args = parser.parse_args()
    try:
        if args.login is None and (args.stories or args.stories_only):
            print("--login=USERNAME required to download stories.", file=sys.stderr)
            args.stories = False
            if args.stories_only:
                raise SystemExit(1)

        if ':feed-all' in args.profile or ':feed-liked' in args.profile:
            raise SystemExit(":feed-all and :feed-liked were removed. Use :feed as target and "
                             "eventually --only-if=viewer_has_liked.")

        download_videos = Tristate.always if not args.no_videos else Tristate.no_extra_query
        download_video_thumbnails = Tristate.always if not args.no_video_thumbnails else Tristate.never
        download_comments = Tristate.always if args.comments else Tristate.no_extra_query
        save_captions = Tristate.no_extra_query if not args.no_captions else Tristate.never
        save_metadata = Tristate.always if args.metadata_json else Tristate.never

        if args.geotags and args.no_geotags:
            raise SystemExit("--geotags and --no-geotags given. I am confused and refuse to work.")
        elif args.geotags:
            download_geotags = Tristate.always
        elif args.no_geotags:
            download_geotags = Tristate.never
        else:
            download_geotags = Tristate.no_extra_query

        loader = Instaloader(sleep=not args.no_sleep, quiet=args.quiet,
                             user_agent=args.user_agent,
                             dirname_pattern=args.dirname_pattern, filename_pattern=args.filename_pattern,
                             download_videos=download_videos, download_video_thumbnails=download_video_thumbnails,
                             download_geotags=download_geotags,
                             save_captions=save_captions, download_comments=download_comments,
                             save_metadata=save_metadata, max_connection_attempts=args.max_connection_attempts)
        loader.main(args.profile,
                    username=args.login.lower() if args.login is not None else None,
                    password=args.password,
                    sessionfile=args.sessionfile,
                    max_count=int(args.count) if args.count is not None else None,
                    profile_pic=not args.no_profile_pic,
                    profile_pic_only=args.profile_pic_only,
                    fast_update=args.fast_update,
                    stories=args.stories,
                    stories_only=args.stories_only,
                    filter_str=args.only_if)
        loader.close()
    except InstaloaderException as err:
        raise SystemExit("Fatal error: %s" % err)


if __name__ == "__main__":
    main()
