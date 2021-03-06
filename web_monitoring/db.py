# This module implements Python classes that provide a restricted and validated
# interface to several databases.

# Pages: associates a URL with agency metadata
# Versions: assocates an HTML version at a specific time with a Page
# Diffs: Catalogs PageFreezer (or PageFreezer-like) result for a given pair of
#        Versions
# Annotations: Human-entered or machine-generated information about a given
#              pair of Versions
#
import os
import tempfile
import logging
import datetime
import collections
import uuid
import json
import hashlib
import time
import csv

import requests
import sqlalchemy

logger = logging.getLogger(__name__)

# These schemas were informed by work by @Mr0grog at
# https://github.com/edgi-govdata-archiving/webpage-versions-db/blob/master/db/schema.rb
PAGES_COLUMNS = (
    sqlalchemy.Column('uuid', sqlalchemy.Text, primary_key=True),
    sqlalchemy.Column('url', sqlalchemy.Text),
    sqlalchemy.Column('title', sqlalchemy.Text),  # <title> tag
    sqlalchemy.Column('agency', sqlalchemy.Text),
    sqlalchemy.Column('site', sqlalchemy.Text),
    sqlalchemy.Column('created_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
    sqlalchemy.Column('updated_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
)
VERSIONS_COLUMNS = (
    sqlalchemy.Column('uuid', sqlalchemy.Text, primary_key=True),
    sqlalchemy.Column('page_uuid', sqlalchemy.Text),
    sqlalchemy.Column('capture_time', sqlalchemy.DateTime),
    sqlalchemy.Column('uri', sqlalchemy.Text),
    sqlalchemy.Column('version_hash', sqlalchemy.Text),
    sqlalchemy.Column('source_type', sqlalchemy.Text),  # e.g., 'PageFreezer'
    sqlalchemy.Column('source_metadata', sqlalchemy.JSON),
    sqlalchemy.Column('created_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
    sqlalchemy.Column('updated_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
)
DIFFS_COLUMNS = (
    sqlalchemy.Column('uuid', sqlalchemy.Text, primary_key=True),
    sqlalchemy.Column('version_from', sqlalchemy.Text),
    sqlalchemy.Column('version_to', sqlalchemy.Text),
    sqlalchemy.Column('diffhash', sqlalchemy.Text),
    sqlalchemy.Column('uri', sqlalchemy.Text),  # filepath, S3 bucket, etc.
    sqlalchemy.Column('source_type', sqlalchemy.Text),
    sqlalchemy.Column('source_metadata', sqlalchemy.JSON),
    sqlalchemy.Column('created_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
    sqlalchemy.Column('updated_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
)
ANNOTATIONS_COLUMNS = (
    sqlalchemy.Column('uuid', sqlalchemy.Text, primary_key=True),
    sqlalchemy.Column('version_from', sqlalchemy.Text),
    sqlalchemy.Column('version_to', sqlalchemy.Text),
    sqlalchemy.Column('annotation', sqlalchemy.types.JSON),
    sqlalchemy.Column('author', sqlalchemy.Text),
    sqlalchemy.Column('created_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
    sqlalchemy.Column('updated_at', sqlalchemy.DateTime,
                      default=datetime.datetime.utcnow),
)


def create(engine):
    meta = sqlalchemy.MetaData(engine)
    sqlalchemy.Table("Pages", meta, *PAGES_COLUMNS)
    sqlalchemy.Table("Versions", meta, *VERSIONS_COLUMNS)
    sqlalchemy.Table("Diffs", meta, *DIFFS_COLUMNS)
    sqlalchemy.Table("Annotations", meta, *ANNOTATIONS_COLUMNS)
    meta.create_all()


class Pages:
    """
    Interface to a table associating a URL with agency metadata.

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
    """
    nt = collections.namedtuple('Page', 'uuid url title agency site')
    def __init__(self, engine):
        self.engine = engine
        meta = sqlalchemy.MetaData(engine)
        self.table = sqlalchemy.Table('Pages', meta, autoload=True)

    def insert(self, url, title, agency, site):
        """
        Insert a new Page into the database.

        This is a prerequisite to storing any Versions of the Page.

        Parameters
        ----------
        url : string
        title : string
        agency : string
        site : string

        Returns
        -------
        uuid : string
            unique identifer assigned to this Page
        """
        _uuid = str(uuid.uuid4())
        now = datetime.datetime.utcnow()
        values = (_uuid, url, title, agency, site, now, now)
        self.engine.execute(self.table.insert().values(values))
        return _uuid

    def __getitem__(self, uuid):
        "Look up a Page by its uuid."
        result = self.engine.execute(
            self.table.select().where(self.table.c.uuid == uuid)).fetchone()
        return self.nt(*result[:-2])

    def by_url(self, url):
        """
        Find a Page by its url.
        """
        proxy = self.engine.execute(
            self.table.select()
            .where(self.table.c.url == url))
        result = proxy.fetchone()
        return self.nt(*result[:-2])


class Versions:
    """
    Interface to a table associating an HTML version at some time with a Page.

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
    """
    unprocessed = collections.deque()
    nt = collections.namedtuple('Version', 'uuid page_uuid capture_time uri '
                                           'version_hash source_time '
                                           'source_metadata')

    def __init__(self, engine):
        self.engine = engine
        meta = sqlalchemy.MetaData(engine)
        self.table = sqlalchemy.Table('Versions', meta, autoload=True)

    def insert(self, page_uuid, capture_time, uri, version_hash, source_type,
               source_metadata):
        """
        Insert a new Version into the database.

        Parameters
        ----------
        page_uuid : string
            referring to the Page that corresponds to this Version
        capture_time : datetime.datetime
        uri : string
            e.g., filepath to HTML file on disk or some other resource locator
        source_type : string
        source_metadata : dict

        Returns
        -------
        uuid : string
            unique identifer assigned to this Version
        """
        _uuid = str(uuid.uuid4())
        now = datetime.datetime.utcnow()
        values = (_uuid, page_uuid, capture_time, uri, version_hash,
                  source_type, source_metadata, now, now)
        self.engine.execute(self.table.insert().values(values))
        self.unprocessed.append(_uuid)
        return _uuid

    def __getitem__(self, uuid):
        "Look up a Version by its uuid."
        result = self.engine.execute(
            self.table.select().where(self.table.c.uuid == uuid)).fetchone()
        return self.nt(*result[:-2])

    def history(self, page_uuid):
        """
        Lazily yield Versions for a given Page in reverse chronological order.
        """
        proxy = self.engine.execute(
            self.table.select()
            .where(self.table.c.page_uuid == page_uuid)
            .order_by(sqlalchemy.desc(self.table.c.capture_time)))
        while True:
            result = proxy.fetchone()
            if result is None:
                raise StopIteration
            yield self.nt(*result[:-2])

    def oldest(self, page_uuid):
        """
        Return the oldest Version for a given Page.
        """
        proxy = self.engine.execute(
            self.table.select()
            .where(self.table.c.page_uuid == page_uuid)
            .order_by(self.table.c.capture_time))
        result = proxy.fetchone()
        return self.nt(*result[:-2])


class Diffs:
    """
    Interface to an object store of PageFreezer(-like) results.

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
    """
    unprocessed = collections.deque()
    nt = collections.namedtuple('Diff', 'uuid version_from version_to '
                                        'diffhash uri '
                                        'source_type source_metadata content')

    def __init__(self, engine):
        self.engine = engine
        meta = sqlalchemy.MetaData(engine)
        self.table = sqlalchemy.Table('Diffs', meta, autoload=True)

    def _get_new_filepath(self):
        "Get a path to save a result JSON to."
        return tempfile.NamedTemporaryFile(delete=False).name

    def insert(self, version_from, version_to, result, source_type,
               source_metadata):
        """
        Insert a new Page into the database.

        This is a prerequisite to storing any Versions of the Page.

        Parameters
        ----------
        version_from : string
            uuid of Version 'before'
        version_to : string
            uuid of Version 'after'
        result : dict
            a JSON blob which will be stashed in a file
        source_type : string
        source_metadata : dict
        """
        diffs = result['output']['diffs']
        diffhash = hashlib.sha256(str(diffs).encode()).hexdigest()
        _uuid = str(uuid.uuid4())
        filepath = self._get_new_filepath()
        with open(filepath, 'w') as f:
            json.dump(result, f)
        now = datetime.datetime.utcnow()
        values = (_uuid, version_from, version_to, diffhash, filepath,
                  source_type, source_metadata, now, now)
        self.engine.execute(self.table.insert().values(values))
        self.unprocessed.append(_uuid)
        return _uuid

    def __getitem__(self, uuid):
        "Look up a Diff by its uuid."
        result = self.engine.execute(
            self.table.select().where(self.table.c.uuid == uuid)).fetchone()
        # For now assume the URI is a filepath. Later we can generalize.
        d = self.nt(*result[:-2] + (None,))  # None is placeholder for content
        path = d.uri
        with open(path) as f:
            content = json.load(f)
        return d._replace(content=content)


class Annotations:
    """
    Interface to an object store of human-entered information about changes.

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
    """
    nt = collections.namedtuple('Annotation', 'uuid version_from version_to '
                                              'annotation author')

    def __init__(self, engine):
        self.engine = engine
        meta = sqlalchemy.MetaData(engine)
        self.table = sqlalchemy.Table('Annotations', meta, autoload=True)

    def insert(self, version_from, version_to, annotation, author):
        """
        Record an annotation about the Diff with the given uuid.

        Parameters
        ----------
        version_from : string
            uuid of Version 'before'
        version_to : string
            uuid of Version 'after'
        annotation : dict
            i.e., a JSON-like blob
        author : string
            user who submitted the annotation

        Returns
        -------
        uuid : string
            unique identifer assigned to this Page
        """
        _uuid = str(uuid.uuid4())
        now = datetime.datetime.utcnow()
        values = (_uuid, version_from, version_to, annotation, now, now)
        self.engine.execute(self.table.insert().values(values))
        return _uuid

    def __getitem__(self, uuid):
        "Look up an Annotation by its uuid."
        result = self.engine.execute(
            self.table.select().where(self.table.c.uuid == uuid)).fetchone()
        return self.nt(*result[:-2])

    def by_change(self, version_from, version_to):
        "Look up a list of all Annotations for a given change."
        results = self.engine.execute(
            self.table.select()
            .where((self.table.c.version_from == version_from) and
                   (self.table.c.version_to == version_to))).fetchall()
        return [self.nt(*result[:-2]) for result in results]


class WorkQueue:
    def __init__(self, priorities, diffs):
        self.priorities = priorities
        self.diffs = diffs
        self.checked_out = {}  # maps user_id to diff_uuid

    def checkout_next(self, user_id):
        for diff_uuid in self.priorities:
            if diff_uuid not in self.checked_out.values():
                # This is the highest-priority Diff not yet checked out.
                self.checked_out[user_id] = diff_uuid
                return self.diffs[diff_uuid]
        raise EmptyWorkQueue("All work is complete or checkout out.")
        
    def checkout(self, user_id, diff_uuid):
        """
        Mark this Diff as being worked on by someone currently.
        """
        if user_id in self.checked_out:
            self.checkin(user_id)
        self.checked_out[user_id] = diff_uuid
        return self.diffs[diff_uuid]

    def checkin(self, user_id):
        """
        Mark this Diff as not being worked on and not complete.
        """
        self.checked_out.pop(user_id)


def compare(html1, html2):
    """
    Send a request to PageFreezer to compare two HTML snippets.

    Parameters
    ----------
    html1 : string
    html2 : string

    Returns
    -------
    response : dict
    """
    URL = 'https://api1.pagefreezer.com/v1/api/utils/diff/compare'
    data = {'source': 'text',
            'url1': html1,
            'url2': html2}
    headers = {'x-api-key': os.environ['PAGE_FREEZER_API_KEY'],
               'Accept': 'application/json',
               'Content-Type': 'application/json', }
    logger.debug("Sending PageFreezer request...")
    raw_response = requests.post(URL, data=json.dumps(data), headers=headers)
    response = raw_response.json()
    logger.debug("Response received in %.3f seconds with status %s.",
                 response.get('elapsed'), response.get('status'))
    return response


def diff_version(version_uuid, versions, diffs, source_type,
                 source_metadata):
    """
    Compare a version with its ancestor and store the result in Diffs.

    It might be convenient to use ``functools.partial`` to bind this to
    specific instances of Versions and Diffs.

    Parameters
    ----------
    version_uuid : string
    versions : Versions
    diffs : Diffs
    source_type : string
    source_metadata : dict
    """
    # Retrieve the Version for the database.
    version = versions[version_uuid]

    # Find its ancestor to compare with.
    ancestor = versions.oldest(version.page_uuid)
    if ancestor == version:
        # This is the oldest one we have -- nothing to compare!
        raise NoAncestor("This is the oldest Version available for the Page "
                         "with page_uuid={}".format(version.page_uuid))
    # Assume uri is a filepath for now. Generalize this later.
    html1 = open(ancestor.uri).read()
    html2 = open(version.uri).read()
    result = compare(html1, html2)  # PageFreezer API call
    if result['status'] != 'ok':
        raise PageFreezerError("result status is not 'ok': {}"
                                "".format(result['status']))
    diffs.insert(ancestor.uuid, version.uuid, result['result'], source_type,
                 source_metadata)


class WebVersioningException(Exception):
    pass
    # All exceptions raised by this package inherit from this.
    #...


class PageFreezerError(WebVersioningException):
    pass
    #...


class NoAncestor(WebVersioningException):
    pass
    #...


class EmptyWorkQueue(WebVersioningException):
    pass
    #...
