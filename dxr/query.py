from itertools import chain, count, groupby
import re
import time

from jinja2 import Markup
from parsimonious import Grammar
from parsimonious.nodes import NodeVisitor

from dxr.extents import flatten_extents, highlight_line

# TODO: Some kind of UI feedback for bad regexes
# TODO: Special argument files-only to just search for file names


# Pattern for matching a file and line number filename:n
_line_number = re.compile("^.*:[0-9]+$")


class Query(object):
    """Query object, constructor will parse any search query"""

    def __init__(self, conn, querystr, should_explain=False, is_case_sensitive=True):
        self.conn = conn
        self._should_explain = should_explain
        self._sql_profile = []
        self.is_case_sensitive = is_case_sensitive

        # A dict with a key for each filter type (like "regexp") in the query.
        # There is also a special "text" key where free text ends up.
        self.terms = QueryVisitor(is_case_sensitive=is_case_sensitive).visit(query_grammar.parse(querystr))

    def single_term(self):
        """Return the single textual term comprising the query.

        If there is more than one term in the query or if the single term is a
        non-textual one, return None.

        """
        if self.terms.keys() == ['text'] and len(self.terms['text']) == 1:
            return self.terms['text'][0]['arg']

    def execute_sql(self, sql, *parameters):
        if self._should_explain:
            self._sql_profile.append({
                "sql" : sql,
                "parameters" : parameters[0] if len(parameters) >= 1 else [],
                "explanation" : self.conn.execute("EXPLAIN QUERY PLAN " + sql, *parameters)
            })
            start_time = time.time()
        res = self.conn.execute(sql, *parameters)
        if self._should_explain:
            # fetch results eagerly so we can get an accurate time for the entire operation
            res = res.fetchall()
            self._sql_profile[-1]["elapsed_time"] = time.time() - start_time
            self._sql_profile[-1]["nrows"] = len(res)
        return res

    def _sql_report(self):
        """Yield a report on how long the SQL I've run has taken."""
        def number_lines(arr):
            ret = []
            for i in range(len(arr)):
                if arr[i] == "":
                    ret.append((i, " "))  # empty lines cause the <div> to collapse and mess up the formatting
                else:
                    ret.append((i, arr[i]))
            return ret

        for i in range(len(self._sql_profile)):
            profile = self._sql_profile[i]
            yield ("",
                          "sql %d (%d row(s); %s seconds)" % (i, profile["nrows"], profile["elapsed_time"]),
                          number_lines(profile["sql"].split("\n")))
            yield ("",
                          "parameters %d" % i,
                          number_lines(map(lambda parm: repr(parm), profile["parameters"])));
            yield ("",
                          "explanation %d" % i,
                          number_lines(map(lambda row: row["detail"], profile["explanation"])))

    def results(self,
                offset=0, limit=100,
                markup='<b>', markdown='</b>'):
        """Return search results as an iterable of these::

            (icon,
             path within tree,
             [(line_number, highlighted_line_of_code), ...])

        """
        sql = ('SELECT %s '
               'FROM %s '
               '%s '
               'ORDER BY %s LIMIT ? OFFSET ?')
        # Filters can add additional fields, in pairs of {extent_start,
        # extent_end}, to be used for highlighting.
        fields = ['files.path', 'files.icon']  # TODO: move extents() to TriliteSearchFilter
        tables = ['files']
        conditions = []
        orderings = ['files.path']
        arguments = []
        has_lines = False

        # Give each registered filter an opportunity to contribute to the
        # query, narrowing it down to the set of matching lines:
        alias_iter = count()
        for f in [filters[0], filters[2]]: # XXX: filters:
            for flds, cond, args in f.filter(self.terms, alias_iter):
                if not has_lines and f.has_lines:
                    has_lines = True
                    # 2 types of query are possible: ones that return just
                    # files and involve no other tables, and ones which join
                    # the lines and trg_index tables and return lines and
                    # extents. This switches from the former to the latter.
                    #
                    # The first time we hit a line-having filter, glom on the
                    # line-based fields. That way, they're always at the
                    # beginning (non-line-having filters never return fields),
                    # so we can use our clever slicing later on to find the
                    # extents fields.
                    fields.extend(['files.encoding', 'files.id as file_id',
                                   'lines.id as line_id', 'lines.number',
                                   'trg_index.text', 'extents(trg_index.contents)'])
                    tables.extend(['lines', 'trg_index'])
                    conditions.extend(['files.id=lines.file_id', 'lines.id=trg_index.id'])
                    orderings.append('lines.number')

                # We fetch the extents for structural filters without doing
                # separate queries, by adding columns to the master search
                # query. Since we're only talking about a line at a time, it is
                # unlikely that there will be multiple highlit extents per
                # filter per line, so any cartesian product of rows can
                # reasonably be absorbed and merged in the app.
                fields.extend(flds)

                conditions.append(cond)
                arguments.extend(args)

        sql %= (', '.join(fields),
                ', '.join(tables),
                ('WHERE ' + ' AND '.join(conditions)) if conditions else '',
                ', '.join(orderings))
        arguments.extend([limit, offset])
        cursor = self.execute_sql(sql, arguments)

        if self._should_explain:
            for r in self._sql_report():
                yield r

        if has_lines:
            # Group lines into files:
            for file_id, fields_and_extents_for_lines in \
                    groupby(flatten_extents(cursor),
                            lambda (fields, extents): fields['file_id']):
                # fields_and_extents_for_lines is [(fields, extents) for one line,
                #                                   ...] for a single file.
                fields_and_extents_for_lines = list(fields_and_extents_for_lines)
                shared_fields = fields_and_extents_for_lines[0][0]  # same for each line in the file

                yield (shared_fields['icon'],
                       shared_fields['path'],
                       [(fields['number'],
                         highlight_line(
                                fields['text'],
                                extents,
                                markup,
                                markdown,
                                shared_fields['encoding']))
                        for fields, extents in fields_and_extents_for_lines])
        else:
            for result in cursor:
                yield (result['icon'],
                       result['path'],
                       [])

        # Boy, as I see what this is doing, I think how good a fit ES is: you fetch a line document, and everything you'd need to highlight is right there. # If var-ref returns 2 extents on one line, it'll just duplicate a line, and we'll merge stuff after the fact. Hey, does that mean I should gather and merge everything before I try to homogenize the extents?
        # Test: If var-ref (or any structural query) returns 2 refs on one line, they should both get highlit.

    def direct_result(self):
        """Return a single search result that is an exact match for the query.

        If there is such a result, return a tuple of (path from root of tree,
        line number). Otherwise, return None.

        """
        term = self.single_term()
        if not term:
            return None
        cur = self.conn.cursor()

        line_number = -1
        if _line_number.match(term):
            parts = term.split(":")
            if len(parts) == 2:
                term = parts[0]
                line_number = int(parts[1])

        # See if we can find only one file match
        cur.execute("""
            SELECT path FROM files WHERE
                path = :term
                OR path LIKE :termPre
            LIMIT 2
        """, {"term": term,
              "termPre": "%/" + term})

        rows = cur.fetchall()
        if rows and len(rows) == 1:
            if line_number >= 0:
                return (rows[0]['path'], line_number)
            return (rows[0]['path'], 1)

        # Case sensitive type matching
        cur.execute("""
            SELECT
                (SELECT path FROM files WHERE files.id = types.file_id) as path,
                types.file_line
              FROM types WHERE types.name = ? LIMIT 2
        """, (term,))
        rows = cur.fetchall()
        if rows and len(rows) == 1:
            return (rows[0]['path'], rows[0]['file_line'])

        # Case sensitive function names
        cur.execute("""
            SELECT
                    (SELECT path FROM files WHERE files.id = functions.file_id) as path,
                    functions.file_line
                FROM functions WHERE functions.name = ? LIMIT 2
        """, (term,))
        rows = cur.fetchall()
        if rows and len(rows) == 1:
            return (rows[0]['path'], rows[0]['file_line'])

        # Try fully qualified names
        if '::' in term:
            # Case insensitive type matching
            cur.execute("""
                SELECT
                      (SELECT path FROM files WHERE files.id = types.file_id) as path,
                      types.file_line
                    FROM types WHERE types.qualname LIKE ? LIMIT 2
            """, (term,))
            rows = cur.fetchall()
            if rows and len(rows) == 1:
                return (rows[0]['path'], rows[0]['file_line'])

            # Case insensitive function names
            cur.execute("""
            SELECT
                  (SELECT path FROM files WHERE files.id = functions.file_id) as path,
                  functions.file_line
                FROM functions WHERE functions.qualname LIKE ? LIMIT 2
            """, (term + '%',))  # Trailing % to eat "(int x)" etc.
            rows = cur.fetchall()
            if rows and len(rows) == 1:
                return (rows[0]['path'], rows[0]['file_line'])

        # Case insensitive type matching
        cur.execute("""
        SELECT
              (SELECT path FROM files WHERE files.id = types.file_id) as path,
              types.file_line
            FROM types WHERE types.name LIKE ? LIMIT 2
        """, (term,))
        rows = cur.fetchall()
        if rows and len(rows) == 1:
            return (rows[0]['path'], rows[0]['file_line'])

        # Case insensitive function names
        cur.execute("""
        SELECT
              (SELECT path FROM files WHERE files.id = functions.file_id) as path,
              functions.file_line
            FROM functions WHERE functions.name LIKE ? LIMIT 2
        """, (term,))
        rows = cur.fetchall()
        if rows and len(rows) == 1:
            return (rows[0]['path'], rows[0]['file_line'])

        # Okay we've got nothing
        return None


def like_escape(val):
    """Escape for usage in as argument to the LIKE operator """
    return (val.replace("\\", "\\\\")
               .replace("_", "\\_")
               .replace("%", "\\%")
               .replace("?", "_")
               .replace("*", "%"))


class SearchFilter(object):
    """Base class for all search filters, plugins subclasses this class and
            registers an instance of them calling register_filter
    """
    # True iff this filter asserts line-based restrictions, shows lines, and
    # highlights text. False for filters that act only on file-level criteria
    # and select no extra SQL fields. This is used to suppress showing all
    # lines of all found files if you do a simple file- based query like
    # ext:html.
    has_lines = True

    def __init__(self, description=''):
        self.description = description

    def filter(self, terms, alias_iter):
        """Yield tuples of (SQL fields, a SQL condition, list of arguments).

        The SQL fields will be added to the SELECT clause and must come in a
        pair taken to be (extent start, extent end). The condition will be
        ANDed into the WHERE clause.

        :arg terms: A dictionary with keys for each filter name I handle (as
            well as others, possibly, which should be ignored). Example::

                {'function': [{'arg': 'o hai',
                               'not': False,
                               'case_sensitive': False,
                               'qualified': False},
                               {'arg': 'what::next',
                                'not': True,
                                'case_sensitive': False,
                                'qualified': True}],
                  ...}
        :arg alias_iter: An iterable that returns numbers available for use in
            table aliases, to keep them unique. Advancing the iterator reserves
            the number it returns.

        """
        return []

    def names(self):
        """Return a list of filter names this filter handles.

        This smooths out the difference between the trilite filter (which
        handles 2 different params) and the other filters (which handle only 1).

        """
        return [self.param] if hasattr(self, 'param') else self.params

    def menu_item(self):
        """Return the item I contribute to the Filters menu.

        Return a dict with ``name`` and ``description`` keys.

        """
        return dict(name=self.param, description=self.description)


class TriliteSearchFilter(SearchFilter):
    params = ['text', 'regexp']

    def filter(self, terms, alias_iter):
        not_conds = []
        not_args  = []
        for term in terms.get('text', []):
            if term['arg']:
                if term['not']:
                    not_conds.append("trg_index.contents MATCH ?")
                    not_args.append(('substr:' if term['case_sensitive']
                                               else 'isubstr:') +
                                    term['arg'])
                else:
                    yield ([],
                           "trg_index.contents MATCH ?",
                           [('substr-extents:' if term['case_sensitive']
                                               else 'isubstr-extents:') +
                            term['arg']])
        for term in terms.get('re', []) + terms.get('regexp', []):
            if term['arg']:
                if term['not']:
                    not_conds.append("trg_index.contents MATCH ?")
                    not_args.append("regexp:" + term['arg'])
                else:
                    yield ([],
                           "trg_index.contents MATCH ?",
                           ["regexp-extents:" + term['arg']])

        if not_conds:
            yield ([],
                   'NOT EXISTS (SELECT 1 FROM trg_index WHERE '
                               'trg_index.id=lines.id AND (%s))' %
                               ' OR '.join(not_conds),
                   not_args)

    def menu_item(self):
        return {'name': 'regexp',
                'description': Markup(r'Regular expression. Examples: <code>regexp:(?i)\bs?printf</code> <code>regexp:"(three|3) mice"</code>')}


class SimpleFilter(SearchFilter):
    """Search filter for limited results.
            This filter take 5 parameters, defined as follows:
                param           Search parameter from query
                filter_sql      Sql condition for limited using argument to param
                neg_filter_sql  Sql condition for limited using argument to param negated.
                formatter       Function/lambda expression for formatting the argument
    """
    has_lines = False  # just happens to be so for all uses at the moment

    def __init__(self, param, filter_sql, neg_filter_sql, formatter, **kwargs):
        super(SimpleFilter, self).__init__(**kwargs)
        self.param = param
        self.filter_sql = filter_sql
        self.neg_filter_sql = neg_filter_sql
        self.formatter = formatter

    def filter(self, terms, alias_iter):
        for term in terms.get(self.param, []):
            arg = term['arg']
            if term['not']:
                yield [], self.neg_filter_sql, self.formatter(arg)
            else:
                yield [], self.filter_sql, self.formatter(arg)


class ExistsLikeFilter(SearchFilter):
    """Search filter for asking of something LIKE this EXISTS,
            This filter takes 5 parameters, param is the search query parameter,
            "-" + param is a assumed to be the negated search filter.
            The filter_sql must be an (SELECT 1 FROM ... WHERE ... %s ...), sql condition on files.id,
            s.t. replacing %s with "qual_name = ?" or "like_name LIKE %?%" where ? is arg given to param
            in search query, and prefixing with EXISTS or NOT EXISTS will yield search
            results as desired :)
            (BTW, did I mention that 'as desired' is awesome way of writing correct specifications)
            ext_sql, must be an sql statement for a list of extent start and end,
            given arguments (file_id, %arg%), where arg is the argument given to
            param. Again %s will be replaced with " = ?" or "LIKE %?%" depending on
            whether or not param is prefixed +
    """
    def __init__(self, param, filter_sql, ext_sql, qual_name, like_name, **kwargs):
        super(ExistsLikeFilter, self).__init__(**kwargs)
        self.param = param
        self.filter_sql = filter_sql
        self.ext_sql = ext_sql
        self.qual_expr = " %s = ? " % qual_name
        self.like_expr = """ %s LIKE ? ESCAPE "\\" """ % like_name

    def filter(self, terms):
        for term in terms.get(self.param, []):
            is_qualified = term['qualified']
            arg = term['arg']
            filter_sql = (self.filter_sql % (self.qual_expr if is_qualified
                                             else self.like_expr))
            sql_params = [arg if is_qualified else like_escape(arg)]
            if term['not']:
                yield 'NOT EXISTS (%s)' % filter_sql, sql_params, False
            else:
                yield 'EXISTS (%s)' % filter_sql, sql_params, self.ext_sql is not None

    def extents(self, terms, execute_sql, file_id):
        def builder():
            for term in terms.get(self.param, []):
                arg = term['arg']
                escaped_arg, sql_expr = (
                    (arg, self.qual_expr) if term['qualified']
                    else (like_escape(arg), self.like_expr))
                for start, end in execute_sql(self.ext_sql % sql_expr,
                                              [file_id, escaped_arg]):
                    # Nones used to occur in the DB. Is this still true?
                    if start and end:
                        yield start, end, []
        if self.ext_sql:
            yield builder()


class UnionFilter(SearchFilter):
    """Provides a filter matching the union of the given filters.

            For when you want OR instead of AND.
    """
    def __init__(self, filters, **kwargs):
        super(UnionFilter, self).__init__(**kwargs)
        # For the moment, UnionFilter supports only single-param filters. There
        # is no reason this can't change.
        unique_params = set(f.param for f in filters)
        if len(unique_params) > 1:
            raise ValueError('All filters that make up a union filter must have the same name, but we got %s.' % ' and '.join(unique_params))
        self.param = unique_params.pop()  # for consistency with other filters
        self.filters = filters

    def filter(self, terms):
        for res in zip(*(filt.filter(terms) for filt in self.filters)):
            yield ('(' + ' OR '.join(conds for (conds, args, exts) in res) + ')',
                   [arg for (conds, args, exts) in res for arg in args],
                   any(exts for (conds, args, exts) in res))

    def extents(self, terms, execute_sql, file_id):
        def builder():
            for filt in self.filters:
                for hits in filt.extents(terms, execute_sql, file_id):
                    for hit in hits:
                        yield hit
        def sorter():
            for hits in groupby(sorted(builder())):
                yield hits[0]
        yield sorter()


# Register filters by adding them to this list:
filters = [
    # path filter
    # TODO: Don't show every line of every file we find if we're just using the
    # path filter--or the path and ext filters--alone. Don't even join up the
    # lines and trg_index tables...or something. ext_sql used to effectively act as a special flag for this; it was set to None in these 2 filters. We could add a show_lines=False to these (and a show_lines=True to the others) and only join up the other tables if there's a True one in the query.
    SimpleFilter(
        param             = "path",
        description       = Markup('File or directory sub-path to search within. <code>*</code> and <code>?</code> act as shell wildcards.'),
        filter_sql        = """files.path LIKE ? ESCAPE "\\" """,
        neg_filter_sql    = """files.path NOT LIKE ? ESCAPE "\\" """,
        formatter         = lambda arg: ['%' + like_escape(arg) + '%']
    ),

    # ext filter
    SimpleFilter(
        param             = "ext",
        description       = Markup('Filename extension: <code>ext:cpp</code>'),
        filter_sql        = """files.path LIKE ? ESCAPE "\\" """,
        neg_filter_sql    = """files.path NOT LIKE ? ESCAPE "\\" """,
        formatter         = lambda arg: ['%' +
            like_escape(arg if arg.startswith(".") else "." + arg)]
    ),

    TriliteSearchFilter(),

    # function filter
    ExistsLikeFilter(
        description   = Markup('Function or method definition: <code>function:foo</code>'),
        param         = "function",
        filter_sql    = """SELECT 1 FROM functions
                           WHERE %s
                             AND functions.file_id = files.id
                        """,
        ext_sql       = """SELECT functions.extent_start, functions.extent_end FROM functions
                           WHERE functions.file_id = ?
                             AND %s
                           ORDER BY functions.extent_start
                        """,  # XXX: We'll have to have the plugins framework or something translate the file offsets from the plugins into line offsets. It'll probably be of symmetric horribleness with the line-walking crap we'll delete from this file, but at least it'll happen at build time rather than for every request.
        like_name     = "functions.name",
        qual_name     = "functions.qualname"
    ),

    # function-ref filter
    ExistsLikeFilter(
        description   = 'Function or method references',
        param         = "function-ref",
        filter_sql    = """SELECT 1 FROM functions, function_refs AS refs
                           WHERE %s
                             AND functions.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM function_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM functions
                                         WHERE %s
                                           AND functions.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "functions.name",
        qual_name     = "functions.qualname"
    ),

    # function-decl filter
    ExistsLikeFilter(
        description   = 'Function or method declaration',
        param         = "function-decl",
        filter_sql    = """SELECT 1 FROM functions, function_decldef as decldef
                           WHERE %s
                             AND functions.id = decldef.defid AND decldef.file_id = files.id
                        """,
        ext_sql       = """SELECT decldef.extent_start, decldef.extent_end FROM function_decldef AS decldef
                           WHERE decldef.file_id = ?
                             AND EXISTS (SELECT 1 FROM functions
                                         WHERE %s
                                           AND functions.id = decldef.defid)
                           ORDER BY decldef.extent_start
                        """,
        like_name     = "functions.name",
        qual_name     = "functions.qualname"
    ),

    UnionFilter([
      # callers filter (direct-calls)
      ExistsLikeFilter(
          param         = "callers",
          filter_sql    = """SELECT 1
                              FROM functions as caller, functions as target, callers
                             WHERE %s
                               AND callers.targetid = target.id
                               AND callers.callerid = caller.id
                               AND caller.file_id = files.id
                          """,
          ext_sql       = """SELECT functions.extent_start, functions.extent_end
                              FROM functions
                             WHERE functions.file_id = ?
                               AND EXISTS (SELECT 1 FROM functions as target, callers
                                            WHERE %s
                                              AND callers.targetid = target.id
                                              AND callers.callerid = functions.id
                                          )
                             ORDER BY functions.extent_start
                          """,
          like_name     = "target.name",
          qual_name     = "target.qualname"
      ),

      # callers filter (indirect-calls)
      ExistsLikeFilter(
          param         = "callers",
          filter_sql    = """SELECT 1
                              FROM functions as caller, functions as target, callers, targets
                             WHERE %s
                               AND targets.funcid = target.id
                               AND targets.targetid = callers.targetid
                               AND callers.callerid = caller.id
                               AND caller.file_id = files.id
                          """,
          ext_sql       = """SELECT functions.extent_start, functions.extent_end
                              FROM functions
                             WHERE functions.file_id = ?
                               AND EXISTS (SELECT 1 FROM functions as target, callers, targets
                                            WHERE %s
                                              AND targets.funcid = target.id
                                              AND targets.targetid = callers.targetid
                                              AND callers.callerid = functions.id
                                          )
                             ORDER BY functions.extent_start
                          """,
          like_name     = "target.name",
          qual_name     = "target.qualname")],

      description = Markup('Functions which call the given function or method: <code>callers:GetStringFromName</code>')
    ),

    UnionFilter([
      # called-by filter (direct calls)
      ExistsLikeFilter(
          param         = "called-by",
          filter_sql    = """SELECT 1
                               FROM functions as target, functions as caller, callers
                              WHERE %s
                                AND callers.callerid = caller.id
                                AND callers.targetid = target.id
                                AND target.file_id = files.id
                          """,
          ext_sql       = """SELECT functions.extent_start, functions.extent_end
                              FROM functions
                             WHERE functions.file_id = ?
                               AND EXISTS (SELECT 1 FROM functions as caller, callers
                                            WHERE %s
                                              AND caller.id = callers.callerid
                                              AND callers.targetid = functions.id
                                          )
                             ORDER BY functions.extent_start
                          """,
          like_name     = "caller.name",
          qual_name     = "caller.qualname"
      ),

      # called-by filter (indirect calls)
      ExistsLikeFilter(
          param         = "called-by",
          filter_sql    = """SELECT 1
                               FROM functions as target, functions as caller, callers, targets
                              WHERE %s
                                AND callers.callerid = caller.id
                                AND targets.funcid = target.id
                                AND targets.targetid = callers.targetid
                                AND target.file_id = files.id
                          """,
          ext_sql       = """SELECT functions.extent_start, functions.extent_end
                              FROM functions
                             WHERE functions.file_id = ?
                               AND EXISTS (SELECT 1 FROM functions as caller, callers, targets
                                            WHERE %s
                                              AND caller.id = callers.callerid
                                              AND targets.funcid = functions.id
                                              AND targets.targetid = callers.targetid
                                          )
                             ORDER BY functions.extent_start
                          """,
          like_name     = "caller.name",
          qual_name     = "caller.qualname"
      )],

      description = 'Functions or methods which are called by the given one'
    ),

    # type filter
    UnionFilter([
      ExistsLikeFilter(
        param         = "type",
        filter_sql    = """SELECT 1 FROM types
                           WHERE %s
                             AND types.file_id = files.id
                        """,
        ext_sql       = """SELECT types.extent_start, types.extent_end FROM types
                           WHERE types.file_id = ?
                             AND %s
                           ORDER BY types.extent_start
                        """,
        like_name     = "types.name",
        qual_name     = "types.qualname"
      ),
      ExistsLikeFilter(
        param         = "type",
        filter_sql    = """SELECT 1 FROM typedefs
                           WHERE %s
                             AND typedefs.file_id = files.id
                        """,
        ext_sql       = """SELECT typedefs.extent_start, typedefs.extent_end FROM typedefs
                           WHERE typedefs.file_id = ?
                             AND %s
                           ORDER BY typedefs.extent_start
                        """,
        like_name     = "typedefs.name",
        qual_name     = "typedefs.qualname")],
      description=Markup('Type or class definition: <code>type:Stack</code>')
    ),

    # type-ref filter
    UnionFilter([
      ExistsLikeFilter(
        param         = "type-ref",
        filter_sql    = """SELECT 1 FROM types, type_refs AS refs
                           WHERE %s
                             AND types.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM type_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM types
                                         WHERE %s
                                           AND types.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "types.name",
        qual_name     = "types.qualname"
      ),
      ExistsLikeFilter(
        param         = "type-ref",
        filter_sql    = """SELECT 1 FROM typedefs, typedef_refs AS refs
                           WHERE %s
                             AND typedefs.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM typedef_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM typedefs
                                         WHERE %s
                                           AND typedefs.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "typedefs.name",
        qual_name     = "typedefs.qualname")],
      description='Type or class references, uses, or instantiations'
    ),

    # type-decl filter
    ExistsLikeFilter(
      description   = 'Type or class declaration',
      param         = "type-decl",
      filter_sql    = """SELECT 1 FROM types, type_decldef AS decldef
                         WHERE %s
                           AND types.id = decldef.defid AND decldef.file_id = files.id
                      """,
      ext_sql       = """SELECT decldef.extent_start, decldef.extent_end FROM type_decldef AS decldef
                         WHERE decldef.file_id = ?
                           AND EXISTS (SELECT 1 FROM types
                                       WHERE %s
                                         AND types.id = decldef.defid)
                         ORDER BY decldef.extent_start
                      """,
      like_name     = "types.name",
      qual_name     = "types.qualname"
    ),

    # var filter
    ExistsLikeFilter(
        description   = 'Variable definition',
        param         = "var",
        filter_sql    = """SELECT 1 FROM variables
                           WHERE %s
                             AND variables.file_id = files.id
                        """,
        ext_sql       = """SELECT variables.extent_start, variables.extent_end FROM variables
                           WHERE variables.file_id = ?
                             AND %s
                           ORDER BY variables.extent_start
                        """,
        like_name     = "variables.name",
        qual_name     = "variables.qualname"
    ),

    # var-ref filter
    ExistsLikeFilter(
        description   = 'Variable uses (lvalue, rvalue, dereference, etc.)',
        param         = "var-ref",
        filter_sql    = """SELECT 1 FROM variables, variable_refs AS refs
                           WHERE %s
                             AND variables.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM variable_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM variables
                                         WHERE %s
                                           AND variables.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "variables.name",
        qual_name     = "variables.qualname"
    ),

    # var-decl filter
    ExistsLikeFilter(
        description   = 'Variable declaration',
        param         = "var-decl",
        filter_sql    = """SELECT 1 FROM variables, variable_decldef AS decldef
                           WHERE %s
                             AND variables.id = decldef.defid AND decldef.file_id = files.id
                        """,
        ext_sql       = """SELECT decldef.extent_start, decldef.extent_end FROM variable_decldef AS decldef
                           WHERE decldef.file_id = ?
                             AND EXISTS (SELECT 1 FROM variables
                                         WHERE %s
                                           AND variables.id = decldef.defid)
                           ORDER BY decldef.extent_start
                        """,
        like_name     = "variables.name",
        qual_name     = "variables.qualname"
    ),

    # macro filter
    ExistsLikeFilter(
        description   = 'Macro definition',
        param         = "macro",
        filter_sql    = """SELECT 1 FROM macros
                           WHERE %s
                             AND macros.file_id = files.id
                        """,
        ext_sql       = """SELECT macros.extent_start, macros.extent_end FROM macros
                           WHERE macros.file_id = ?
                             AND %s
                           ORDER BY macros.extent_start
                        """,
        like_name     = "macros.name",
        qual_name     = "macros.name"
    ),

    # macro-ref filter
    ExistsLikeFilter(
        description   = 'Macro uses',
        param         = "macro-ref",
        filter_sql    = """SELECT 1 FROM macros, macro_refs AS refs
                           WHERE %s
                             AND macros.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM macro_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM macros
                                         WHERE %s
                                           AND macros.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "macros.name",
        qual_name     = "macros.name"
    ),

    # namespace filter
    ExistsLikeFilter(
        description   = 'Namespace definition',
        param         = "namespace",
        filter_sql    = """SELECT 1 FROM namespaces
                           WHERE %s
                             AND namespaces.file_id = files.id
                        """,
        ext_sql       = """SELECT namespaces.extent_start, namespaces.extent_end FROM namespaces
                           WHERE namespaces.file_id = ?
                             AND %s
                           ORDER BY namespaces.extent_start
                        """,
        like_name     = "namespaces.name",
        qual_name     = "namespaces.qualname"
    ),

    # namespace-ref filter
    ExistsLikeFilter(
        description   = 'Namespace references',
        param         = "namespace-ref",
        filter_sql    = """SELECT 1 FROM namespaces, namespace_refs AS refs
                           WHERE %s
                             AND namespaces.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM namespace_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM namespaces
                                         WHERE %s
                                           AND namespaces.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "namespaces.name",
        qual_name     = "namespaces.qualname"
    ),

    # namespace-alias filter
    ExistsLikeFilter(
        description   = 'Namespace alias',
        param         = "namespace-alias",
        filter_sql    = """SELECT 1 FROM namespace_aliases
                           WHERE %s
                             AND namespace_aliases.file_id = files.id
                        """,
        ext_sql       = """SELECT namespace_aliases.extent_start, namespace_aliases.extent_end FROM namespace_aliases
                           WHERE namespace_aliases.file_id = ?
                             AND %s
                           ORDER BY namespace_aliases.extent_start
                        """,
        like_name     = "namespace_aliases.name",
        qual_name     = "namespace_aliases.qualname"
    ),

    # namespace-alias-ref filter
    ExistsLikeFilter(
        description   = 'Namespace alias references',
        param         = "namespace-alias-ref",
        filter_sql    = """SELECT 1 FROM namespace_aliases, namespace_alias_refs AS refs
                           WHERE %s
                             AND namespace_aliases.id = refs.refid AND refs.file_id = files.id
                        """,
        ext_sql       = """SELECT refs.extent_start, refs.extent_end FROM namespace_alias_refs AS refs
                           WHERE refs.file_id = ?
                             AND EXISTS (SELECT 1 FROM namespace_aliases
                                         WHERE %s
                                           AND namespace_aliases.id = refs.refid)
                           ORDER BY refs.extent_start
                        """,
        like_name     = "namespace_aliases.name",
        qual_name     = "namespace_aliases.qualname"
    ),

    # bases filter -- reorder these things so more frequent at top.
    ExistsLikeFilter(
        description   = Markup('Superclasses of a class: <code>bases:SomeSubclass</code>'),
        param         = "bases",
        filter_sql    = """SELECT 1 FROM types as base, impl, types
                            WHERE %s
                              AND impl.tbase = base.id
                              AND impl.tderived = types.id
                              AND base.file_id = files.id""",
        ext_sql       = """SELECT base.extent_start, base.extent_end
                            FROM types as base
                           WHERE base.file_id = ?
                             AND EXISTS (SELECT 1 FROM impl, types
                                         WHERE impl.tbase = base.id
                                           AND impl.tderived = types.id
                                           AND %s
                                        )
                        """,
        like_name     = "types.name",
        qual_name     = "types.qualname"
    ),

    # derived filter
    ExistsLikeFilter(
        description   = Markup('Subclasses of a class: <code>derived:SomeSuperclass</code>'),
        param         = "derived",
        filter_sql    = """SELECT 1 FROM types as sub, impl, types
                            WHERE %s
                              AND impl.tbase = types.id
                              AND impl.tderived = sub.id
                              AND sub.file_id = files.id""",
        ext_sql       = """SELECT sub.extent_start, sub.extent_end
                            FROM types as sub
                           WHERE sub.file_id = ?
                             AND EXISTS (SELECT 1 FROM impl, types
                                         WHERE impl.tbase = types.id
                                           AND impl.tderived = sub.id
                                           AND %s
                                        )
                        """,
        like_name     = "types.name",
        qual_name     = "types.qualname"
    ),

    UnionFilter([
      # member filter for functions
      ExistsLikeFilter(
        param         = "member",
        filter_sql    = """SELECT 1 FROM types as type, functions as mem
                            WHERE %s
                              AND mem.scopeid = type.id AND mem.file_id = files.id
                        """,
        ext_sql       = """ SELECT extent_start, extent_end
                              FROM functions as mem WHERE mem.file_id = ?
                                      AND EXISTS ( SELECT 1 FROM types as type
                                                    WHERE %s
                                                      AND type.id = mem.scopeid)
                           ORDER BY mem.extent_start
                        """,
        like_name     = "type.name",
        qual_name     = "type.qualname"
      ),
      # member filter for types
      ExistsLikeFilter(
        param         = "member",
        filter_sql    = """SELECT 1 FROM types as type, types as mem
                            WHERE %s
                              AND mem.scopeid = type.id AND mem.file_id = files.id
                        """,
        ext_sql       = """ SELECT extent_start, extent_end
                              FROM types as mem WHERE mem.file_id = ?
                                      AND EXISTS ( SELECT 1 FROM types as type
                                                    WHERE %s
                                                      AND type.id = mem.scopeid)
                           ORDER BY mem.extent_start
                        """,
        like_name     = "type.name",
        qual_name     = "type.qualname"
      ),
      # member filter for variables
      ExistsLikeFilter(
        param         = "member",
        filter_sql    = """SELECT 1 FROM types as type, variables as mem
                            WHERE %s
                              AND mem.scopeid = type.id AND mem.file_id = files.id
                        """,
        ext_sql       = """ SELECT extent_start, extent_end
                              FROM variables as mem WHERE mem.file_id = ?
                                      AND EXISTS ( SELECT 1 FROM types as type
                                                    WHERE %s
                                                      AND type.id = mem.scopeid)
                           ORDER BY mem.extent_start
                        """,
        like_name     = "type.name",
        qual_name     = "type.qualname")],

      description = Markup('Member variables, types, or methods of a class: <code>member:SomeClass</code>')
    ),

    # overridden filter
    ExistsLikeFilter(
        description   = Markup('Methods which are overridden by the given one. Useful mostly with fully qualified methods, like <code>+overridden:Derived::foo()</code>.'),
        param         = "overridden",
        filter_sql    = """SELECT 1
                             FROM functions as base, functions as derived, targets
                            WHERE %s
                              AND base.id = -targets.targetid
                              AND derived.id = targets.funcid
                              AND base.id <> derived.id
                              AND base.file_id = files.id
                        """,
        ext_sql       = """SELECT functions.extent_start, functions.extent_end
                            FROM functions
                           WHERE functions.file_id = ?
                             AND EXISTS (SELECT 1 FROM functions as derived, targets
                                          WHERE %s
                                            AND functions.id = -targets.targetid
                                            AND derived.id = targets.funcid
                                            AND functions.id <> derived.id
                                        )
                           ORDER BY functions.extent_start
                        """,
        like_name     = "derived.name",
        qual_name     = "derived.qualname"
    ),

    # overrides filter
    ExistsLikeFilter(
        description   = Markup('Methods which override the given one: <code>overrides:someMethod</code>'),
        param         = "overrides",
        filter_sql    = """SELECT 1
                             FROM functions as base, functions as derived, targets
                            WHERE %s
                              AND base.id = -targets.targetid
                              AND derived.id = targets.funcid
                              AND base.id <> derived.id
                              AND derived.file_id = files.id
                        """,
        ext_sql       = """SELECT functions.extent_start, functions.extent_end
                            FROM functions
                           WHERE functions.file_id = ?
                             AND EXISTS (SELECT 1 FROM functions as base, targets
                                          WHERE %s
                                            AND base.id = -targets.targetid
                                            AND functions.id = targets.funcid
                                            AND base.id <> functions.id
                                        )
                           ORDER BY functions.extent_start
                        """,
        like_name     = "base.name",
        qual_name     = "base.qualname"
    ),

    #warning filter
    ExistsLikeFilter(
        description   = 'Compiler warning messages',
        param         = "warning",
        filter_sql    = """SELECT 1 FROM warnings
                            WHERE %s
                              AND warnings.file_id = files.id """,
        ext_sql       = """SELECT warnings.extent_start, warnings.extent_end
                             FROM warnings
                            WHERE warnings.file_id = ?
                              AND %s
                        """,
        like_name     = "warnings.msg",
        qual_name     = "warnings.msg"
    ),

    #warning-opt filter
    ExistsLikeFilter(
        description   = 'More (less severe?) warning messages',
        param         = "warning-opt",
        filter_sql    = """SELECT 1 FROM warnings
                            WHERE %s
                              AND warnings.file_id = files.id """,
        ext_sql       = """SELECT warnings.extent_start, warnings.extent_end
                             FROM warnings
                            WHERE warnings.file_id = ?
                              AND %s
                        """,
        like_name     = "warnings.opt",
        qual_name     = "warnings.opt"
    )
]


query_grammar = Grammar(ur'''
    query = _ term*
    term = not_term / positive_term
    not_term = not positive_term
    positive_term = filtered_term / text

    # A term with a filter name prepended:
    filtered_term = maybe_plus filter ":" text

    # Bare or quoted text, possibly with spaces. Not empty.
    text = (double_quoted_text / single_quoted_text / bare_text) _

    filter = ~r"''' +
        # regexp, function, etc. No filter is a prefix of a later one. This
        # avoids premature matches.
        '|'.join(sorted(chain.from_iterable(map(re.escape, f.names()) for f in filters),
                        key=len,
                        reverse=True)) + ur'''"

    not = "-"

    # You can stick a plus in front of anything, and it'll parse, but it has
    # meaning only with the filters where it makes sense.
    maybe_plus = "+"?

    # Unquoted text until a space or EOL:
    bare_text = ~r"[^ ]+"

    # A string starting with a double quote and extending to {a double quote
    # followed by a space} or {a double quote followed by the end of line} or
    # {simply the end of line}, ignoring (that is, including) backslash-escaped
    # quotes. The intent is to take quoted strings like `"hi \there"woo"` and
    # take a good guess at what you mean even while you're still typing, before
    # you've closed the quote. The motivation for providing backslash-escaping
    # is so you can express trailing quote-space pairs without having the
    # scanner prematurely end.
    double_quoted_text = ~r'"(?P<content>(?:[^"\\]*(?:\\"|\\|"[^ ])*)*)(?:"(?= )|"$|$)'
    # A symmetric rule for single quotes:
    single_quoted_text = ~r"'(?P<content>(?:[^'\\]*(?:\\'|\\|'[^ ])*)*)(?:'(?= )|'$|$)"

    _ = ~r"[ \t]*"
    ''')


class QueryVisitor(NodeVisitor):
    visit_positive_term = NodeVisitor.lift_child

    def __init__(self, is_case_sensitive=False):
        """Construct.

        :arg is_case_sensitive: What "case_sensitive" value to set on every
            term. This is meant to be temporary, until we expose per-term case
            sensitivity to the user.

        """
        super(NodeVisitor, self).__init__()
        self.is_case_sensitive = is_case_sensitive

    def visit_query(self, query, (_, terms)):
        """Group terms into a dict of lists by filter type, and return it."""
        d = {}
        for filter_name, subdict in terms:
            d.setdefault(filter_name, []).append(subdict)
        return d

    def visit_term(self, term, ((filter_name, subdict),)):
        """Set the case-sensitive bit and, if not already set, a default not
        bit."""
        subdict['case_sensitive'] = self.is_case_sensitive
        subdict.setdefault('not', False)
        subdict.setdefault('qualified', False)
        return filter_name, subdict

    def visit_not_term(self, not_term, (not_, (filter_name, subdict))):
        """Add "not" bit to the subdict."""
        subdict['not'] = True
        return filter_name, subdict

    def visit_filtered_term(self, filtered_term, (plus, filter, colon, (text_type, subdict))):
        """Add fully-qualified indicator to the term subdict, and return it and
        the filter name."""
        subdict['qualified'] = plus.text == '+'
        return filter.text, subdict

    def visit_text(self, text, ((some_text,), _)):
        """Create the subdictionary that lives in Query.terms. Return it and
        'text', indicating that this is a bare or quoted run of text. If it is
        actually an argument to a filter, ``visit_filtered_term`` will
        overrule us later.

        """
        return 'text', {'arg': some_text}

    def visit_maybe_plus(self, plus, wtf):
        """Keep the plus from turning into a list half the time. That makes it
        awkward to compare against."""
        return plus

    def visit_bare_text(self, bare_text, visited_children):
        return bare_text.text

    def visit_double_quoted_text(self, quoted_text, visited_children):
        return quoted_text.match.group('content').replace(r'\"', '"')

    def visit_single_quoted_text(self, quoted_text, visited_children):
        return quoted_text.match.group('content').replace(r"\'", "'")

    def generic_visit(self, node, visited_children):
        """Replace childbearing nodes with a list of their children; keep
        others untouched.

        """
        return visited_children or node


def filter_menu_items():
    """Return the additional template variables needed to render filter.html."""
    return (f.menu_item() for f in filters)
