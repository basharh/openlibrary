"""
This file should be for internal APIs which Open Library requires for
its experience. This does not include public facing APIs with LTS
(long term support)
"""

import web
import re
import simplejson

from infogami import config
from infogami.utils import delegate
from infogami.utils.view import render_template  # noqa: F401 used for its side effects
from infogami.plugins.api.code import jsonapi
from infogami.utils.view import add_flash_message
from openlibrary import accounts
from openlibrary.utils.isbn import isbn_10_to_isbn_13, normalize_isbn
from openlibrary.utils import extract_numeric_id_from_olid
from openlibrary.plugins.worksearch.subjects import get_subject
from openlibrary.accounts.model import OpenLibraryAccount
from openlibrary.core import ia, db, models, lending, helpers as h
from openlibrary.core.sponsorships import qualifies_for_sponsorship
from openlibrary.core.vendors import (
    get_amazon_metadata, create_edition_from_amazon_metadata,
    search_amazon, get_betterworldbooks_metadata)


class book_availability(delegate.page):
    path = "/availability/v2"

    def GET(self):
        i = web.input(type='', ids='')
        id_type = i.type
        ids = i.ids.split(',')
        result = self.get_book_availability(id_type, ids)
        return delegate.RawText(simplejson.dumps(result),
                                content_type="application/json")

    def POST(self):
        i = web.input(type='')
        j = simplejson.loads(web.data())
        id_type = i.type
        ids = j.get('ids', [])
        result = self.get_book_availability(id_type, ids)
        return delegate.RawText(simplejson.dumps(result),
                                content_type="application/json")

    def get_book_availability(self, id_type, ids):
        return (
            lending.get_availability_of_works(ids) if id_type == "openlibrary_work"
            else
            lending.get_availability_of_editions(ids) if id_type == "openlibrary_edition"
            else
            lending.get_availability_of_ocaids(ids) if id_type == "identifier"
            else []
        )


class browse(delegate.page):
    path = "/browse"
    encoding = "json"

    def GET(self):
        i = web.input(q='', page=1, limit=100, subject='',
                      work_id='', _type='', sorts='')
        sorts = i.sorts.split(',')
        page = int(i.page)
        limit = int(i.limit)

        print("query: " + i.q)
        print("subject: " + i.subject)
        print("work_id: " + i.work_id)

        url = lending.compose_ia_url(
            query=i.q, limit=limit, page=page, subject=i.subject,
            work_id=i.work_id, _type=i._type, sorts=sorts)
        works = lending.get_available(url=url) if url else []
        result = {
            'query': url,
            'works': [work.dict() for work in works],
        }
        return delegate.RawText(
            simplejson.dumps(result),
            content_type="application/json")


class ratings(delegate.page):
    path = r"/works/OL(\d+)W/ratings"
    encoding = "json"

    def POST(self, work_id):
        """Registers new ratings for this work"""
        user = accounts.get_current_user()
        i = web.input(edition_id=None, rating=None, redir=False)
        key = i.edition_id if i.edition_id else ('/works/OL%sW' % work_id)
        edition_id = int(extract_numeric_id_from_olid(i.edition_id)) if i.edition_id else None

        if not user:
            raise web.seeother('/account/login?redirect=%s' % key)

        username = user.key.split('/')[2]

        def response(msg, status="success"):
            return delegate.RawText(simplejson.dumps({
                status: msg
            }), content_type="application/json")

        if i.rating is None:
            models.Ratings.remove(username, work_id)
            r = response('removed rating')

        else:
            try:
                rating = int(i.rating)
                if rating not in models.Ratings.VALID_STAR_RATINGS:
                    raise ValueError
            except ValueError:
                return response('invalid rating', status="error")

            models.Ratings.add(
                username=username, work_id=work_id,
                rating=rating, edition_id=edition_id)
            r = response('rating added')

        if i.redir:
            raise web.seeother(key)
        return r

# The GET of work_bookshelves, work_ratings, and work_likes should return some summary of likes,
# not a value tied to this logged in user. This is being used as debugging.

class work_bookshelves(delegate.page):
    path = r"/works/OL(\d+)W/bookshelves"
    encoding = "json"

    @jsonapi
    def GET(self, work_id):
        from openlibrary.core.models import Bookshelves

        result = {'counts': {}}
        counts = Bookshelves.get_num_users_by_bookshelf_by_work_id(work_id)
        for (shelf_name, shelf_id) in Bookshelves.PRESET_BOOKSHELVES_JSON.items():
            result['counts'][shelf_name] = counts.get(shelf_id, 0)

        return simplejson.dumps(result)

    def POST(self, work_id):
        """
        Add a work (or a work and an edition) to a bookshelf.
        
        GET params:
        - edition_id str (optional)
        - action str: e.g. "add", "remove"
        - redir bool: if patron not logged in, redirect back to page after login
        - bookshelf_id int: which bookshelf? e.g. the ID for "want to read"?
        - dont_remove bool: if book exists & action== "add", don't try removal  

        :param str work_id: e.g. OL123W
        :rtype: json
        :return: a list of bookshelves_affected
        """
        from openlibrary.core.models import Bookshelves

        user = accounts.get_current_user()
        i = web.input(edition_id=None, action="add", redir=False, bookshelf_id=None, dont_remove=False)
        key = i.edition_id if i.edition_id else ('/works/OL%sW' % work_id)

        if not user:
            raise web.seeother('/account/login?redirect=%s' % key)

        username = user.key.split('/')[2]
        current_status = Bookshelves.get_users_read_status_of_work(username, work_id)

        try:
            bookshelf_id = int(i.bookshelf_id)
            shelf_ids = Bookshelves.PRESET_BOOKSHELVES.values()
            if bookshelf_id != -1 and bookshelf_id not in shelf_ids:
                raise ValueError
        except ValueError:
            return delegate.RawText(simplejson.dumps({
                'error': 'Invalid bookshelf'
            }), content_type="application/json")

        if (not i.dont_remove) and bookshelf_id == current_status or bookshelf_id == -1:
            work_bookshelf = Bookshelves.remove(
                username=username, work_id=work_id, bookshelf_id=current_status)

        else:
            edition_id = int(i.edition_id.split('/')[2][2:-1]) if i.edition_id else None
            work_bookshelf = Bookshelves.add(
                username=username, bookshelf_id=bookshelf_id,
                work_id=work_id, edition_id=edition_id)

        if i.redir:
            raise web.seeother(key)
        return delegate.RawText(simplejson.dumps({
            'bookshelves_affected': work_bookshelf
        }), content_type="application/json")


class work_editions(delegate.page):
    path = r"(/works/OL\d+W)/editions"
    encoding = "json"

    def GET(self, key):
        doc = web.ctx.site.get(key)
        if not doc or doc.type.key != "/type/work":
            raise web.notfound('')
        else:
            i = web.input(limit=50, offset=0)
            limit = h.safeint(i.limit) or 50
            offset = h.safeint(i.offset) or 0

            data = self.get_editions_data(doc, limit=limit, offset=offset)
            return delegate.RawText(simplejson.dumps(data), content_type="application/json")

    def get_editions_data(self, work, limit, offset):
        if limit > 1000:
            limit = 1000

        keys = web.ctx.site.things({"type": "/type/edition", "works": work.key, "limit": limit, "offset": offset})
        editions = web.ctx.site.get_many(keys, raw=True)

        size = work.edition_count
        links = {
            "self": web.ctx.fullpath,
            "work": work.key,
        }

        if offset > 0:
            links['prev'] = web.changequery(offset=min(0, offset-limit))

        if offset + len(editions) < size:
            links['next'] = web.changequery(offset=offset+limit)

        return {
            "links": links,
            "size": size,
            "entries": editions
        }


class author_works(delegate.page):
    path = r"(/authors/OL\d+A)/works"
    encoding = "json"

    def GET(self, key):
        doc = web.ctx.site.get(key)
        if not doc or doc.type.key != "/type/author":
            raise web.notfound('')
        else:
            i = web.input(limit=50, offset=0)
            limit = h.safeint(i.limit) or 50
            offset = h.safeint(i.offset) or 0

            data = self.get_works_data(doc, limit=limit, offset=offset)
            return delegate.RawText(simplejson.dumps(data), content_type="application/json")

    def get_works_data(self, author, limit, offset):
        if limit > 1000:
            limit = 1000

        keys = web.ctx.site.things({"type": "/type/work", "authors": {"author": {"key": author.key}}, "limit": limit, "offset": offset})
        works = web.ctx.site.get_many(keys, raw=True)

        size = author.get_work_count()
        links = {
            "self": web.ctx.fullpath,
            "author": author.key,
        }

        if offset > 0:
            links['prev'] = web.changequery(offset=min(0, offset-limit))

        if offset + len(works) < size:
            links['next'] = web.changequery(offset=offset+limit)

        return {
            "links": links,
            "size": size,
            "entries": works
        }

class amazon_search_api(delegate.page):
    """Librarian + admin only endpoint to check for books
    avaialable on Amazon via the Product Advertising API
    ItemSearch operation.

    https://docs.aws.amazon.com/AWSECommerceService/latest/DG/ItemSearch.html

    Currently experimental to explore what data is avaialable to affiliates.

    :return: JSON {"results": []} containing Amazon product metadata
             for items matching the title and author search parameters.
    :rtype: str
    """

    path = '/_tools/amazon_search'

    @jsonapi
    def GET(self):
        user = accounts.get_current_user()
        if not (user and (user.is_admin() or user.is_librarian())):
            return web.HTTPError('403 Forbidden')
        i = web.input(title='', author='')
        if not (i.author or i.title):
            return simplejson.dumps({
                'error': 'author or title required'
            })
        results = search_amazon(title=i.title, author=i.author)
        return simplejson.dumps(results)

class join_sponsorship_waitlist(delegate.page):
    path = r'/sponsorship/join'

    def GET(self):
        user = accounts.get_current_user()
        if user:
            account = OpenLibraryAccount.get_by_email(user.email)
            ia_itemname = account.itemname if account else None
        if not user or not ia_itemname:
            web.setcookie(config.login_cookie_name, "", expires=-1)
            raise web.seeother("/account/login?redirect=/sponsorship/join")
        try:
            with accounts.RunAs('archive_support'):
                models.UserGroup.from_key('sponsors-waitlist').add_user(user.key)
        except KeyError as e:
            add_flash_message('error', 'Unable to join waitlist: %s' % e.message)

        raise web.seeother('/sponsorship')

class sponsorship_eligibility_check(delegate.page):
    path = r'/sponsorship/eligibility/(.*)'

    @jsonapi
    def GET(self, _id):
        edition = (
            web.ctx.site.get('/books/%s' % _id)
            if re.match(r'OL[0-9]+M', _id)
            else models.Edition.from_isbn(_id)
            
        )
        if not edition:
            return simplejson.dumps({"status": "error", "reason": "Invalid ISBN 13"})
        return simplejson.dumps(qualifies_for_sponsorship(edition))


class price_api(delegate.page):
    path = r'/prices'

    @jsonapi
    def GET(self):
        i = web.input(isbn='', asin='')
        if not (i.isbn or i.asin):
            return simplejson.dumps({
                'error': 'isbn or asin required'
            })
        id_ = i.asin if i.asin else normalize_isbn(i.isbn)
        id_type = 'asin' if i.asin else 'isbn_' + ('13' if len(id_) == 13 else '10')

        metadata = {
            'amazon': get_amazon_metadata(id_, id_type=id_type[:4]) or {},
            'betterworldbooks': get_betterworldbooks_metadata(id_) if id_type.startswith('isbn_') else {}
        }
        # if user supplied isbn_{n} fails for amazon, we may want to check the alternate isbn

        # if bwb fails and isbn10, try again with isbn13
        if id_type == 'isbn_10' and \
           metadata['betterworldbooks'].get('price') is None:
            isbn_13 = isbn_10_to_isbn_13(id_)
            metadata['betterworldbooks'] = isbn_13 and get_betterworldbooks_metadata(
                isbn_13) or {}

        # fetch book by isbn if it exists
        # TODO: perform exisiting OL lookup by ASIN if supplied, if possible
        matches = web.ctx.site.things({
            'type': '/type/edition',
            id_type: id_,
        })

        book_key = matches[0] if matches else None

        # if no OL edition for isbn, attempt to create
        if (not book_key) and metadata.get('amazon'):
            book_key = create_edition_from_amazon_metadata(id_, id_type[:4])

        # include ol edition metadata in response, if available
        if book_key:
            ed = web.ctx.site.get(book_key)
            if ed:
                metadata['key'] = ed.key
                if getattr(ed, 'ocaid'):
                    metadata['ocaid'] = ed.ocaid

        return simplejson.dumps(metadata)
