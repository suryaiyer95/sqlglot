from __future__ import annotations

import typing as t

from sqlglot import exp, generator, parser, tokens, transforms
from sqlglot.dialects.dialect import (
    DATE_ADD_OR_SUB,
    Dialect,
    JSON_EXTRACT_TYPE,
    any_value_to_max_sql,
    bool_xor_sql,
    datestrtodate_sql,
    build_formatted_time,
    filter_array_using_unnest,
    json_extract_segments,
    json_path_key_only_name,
    max_or_greatest,
    merge_without_target_sql,
    min_or_least,
    no_last_day_sql,
    no_map_from_entries_sql,
    no_paren_current_date_sql,
    no_pivot_sql,
    no_trycast_sql,
    build_json_extract_path,
    build_timestamp_trunc,
    rename_func,
    str_position_sql,
    struct_extract_sql,
    timestamptrunc_sql,
    timestrtotime_sql,
    trim_sql,
    ts_or_ds_add_cast,
)
from sqlglot.helper import seq_get
from sqlglot.parser import binary_range_parser
from sqlglot.tokens import TokenType

DATE_DIFF_FACTOR = {
    "MICROSECOND": " * 1000000",
    "MILLISECOND": " * 1000",
    "SECOND": "",
    "MINUTE": " / 60",
    "HOUR": " / 3600",
    "DAY": " / 86400",
}


def _date_add_sql(kind: str) -> t.Callable[[Postgres.Generator, DATE_ADD_OR_SUB], str]:
    def func(self: Postgres.Generator, expression: DATE_ADD_OR_SUB) -> str:
        if isinstance(expression, exp.TsOrDsAdd):
            expression = ts_or_ds_add_cast(expression)

        this = self.sql(expression, "this")
        unit = expression.args.get("unit")

        expression = self._simplify_unless_literal(expression.expression)
        if not isinstance(expression, exp.Literal):
            self.unsupported("Cannot add non literal")

        expression.args["is_string"] = True
        return f"{this} {kind} {self.sql(exp.Interval(this=expression, unit=unit))}"

    return func


def _date_diff_sql(self: Postgres.Generator, expression: exp.DateDiff) -> str:
    unit = expression.text("unit").upper()
    factor = DATE_DIFF_FACTOR.get(unit)

    end = f"CAST({self.sql(expression, 'this')} AS TIMESTAMP)"
    start = f"CAST({self.sql(expression, 'expression')} AS TIMESTAMP)"

    if factor is not None:
        return f"CAST(EXTRACT(epoch FROM {end} - {start}){factor} AS BIGINT)"

    age = f"AGE({end}, {start})"

    if unit == "WEEK":
        unit = f"EXTRACT(days FROM ({end} - {start})) / 7"
    elif unit == "MONTH":
        unit = f"EXTRACT(year FROM {age}) * 12 + EXTRACT(month FROM {age})"
    elif unit == "QUARTER":
        unit = f"EXTRACT(year FROM {age}) * 4 + EXTRACT(month FROM {age}) / 3"
    elif unit == "YEAR":
        unit = f"EXTRACT(year FROM {age})"
    else:
        unit = age

    return f"CAST({unit} AS BIGINT)"


def _substring_sql(self: Postgres.Generator, expression: exp.Substring) -> str:
    this = self.sql(expression, "this")
    start = self.sql(expression, "start")
    length = self.sql(expression, "length")

    from_part = f" FROM {start}" if start else ""
    for_part = f" FOR {length}" if length else ""

    return f"SUBSTRING({this}{from_part}{for_part})"


def _string_agg_sql(self: Postgres.Generator, expression: exp.GroupConcat) -> str:
    separator = expression.args.get("separator") or exp.Literal.string(",")

    order = ""
    this = expression.this
    if isinstance(this, exp.Order):
        if this.this:
            this = this.this.pop()
        order = self.sql(expression.this)  # Order has a leading space

    return f"STRING_AGG({self.format_args(this, separator)}{order})"


def _datatype_sql(self: Postgres.Generator, expression: exp.DataType) -> str:
    if expression.is_type("array"):
        return f"{self.expressions(expression, flat=True)}[]" if expression.expressions else "ARRAY"
    return self.datatype_sql(expression)


def _auto_increment_to_serial(expression: exp.Expression) -> exp.Expression:
    auto = expression.find(exp.AutoIncrementColumnConstraint)

    if auto:
        expression.args["constraints"].remove(auto.parent)
        kind = expression.args["kind"]

        if kind.this == exp.DataType.Type.INT:
            kind.replace(exp.DataType(this=exp.DataType.Type.SERIAL))
        elif kind.this == exp.DataType.Type.SMALLINT:
            kind.replace(exp.DataType(this=exp.DataType.Type.SMALLSERIAL))
        elif kind.this == exp.DataType.Type.BIGINT:
            kind.replace(exp.DataType(this=exp.DataType.Type.BIGSERIAL))

    return expression


def _serial_to_generated(expression: exp.Expression) -> exp.Expression:
    kind = expression.args.get("kind")
    if not kind:
        return expression

    if kind.this == exp.DataType.Type.SERIAL:
        data_type = exp.DataType(this=exp.DataType.Type.INT)
    elif kind.this == exp.DataType.Type.SMALLSERIAL:
        data_type = exp.DataType(this=exp.DataType.Type.SMALLINT)
    elif kind.this == exp.DataType.Type.BIGSERIAL:
        data_type = exp.DataType(this=exp.DataType.Type.BIGINT)
    else:
        data_type = None

    if data_type:
        expression.args["kind"].replace(data_type)
        constraints = expression.args["constraints"]
        generated = exp.ColumnConstraint(kind=exp.GeneratedAsIdentityColumnConstraint(this=False))
        notnull = exp.ColumnConstraint(kind=exp.NotNullColumnConstraint())

        if notnull not in constraints:
            constraints.insert(0, notnull)
        if generated not in constraints:
            constraints.insert(0, generated)

    return expression


def _build_generate_series(args: t.List) -> exp.GenerateSeries:
    # The goal is to convert step values like '1 day' or INTERVAL '1 day' into INTERVAL '1' day
    step = seq_get(args, 2)

    if step is None:
        # Postgres allows calls with just two arguments -- the "step" argument defaults to 1
        return exp.GenerateSeries.from_arg_list(args)

    if step.is_string:
        args[2] = exp.to_interval(step.this)
    elif isinstance(step, exp.Interval) and not step.args.get("unit"):
        args[2] = exp.to_interval(step.this.this)

    return exp.GenerateSeries.from_arg_list(args)


def _build_to_timestamp(args: t.List) -> exp.UnixToTime | exp.StrToTime:
    # TO_TIMESTAMP accepts either a single double argument or (text, text)
    if len(args) == 1:
        # https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-TABLE
        return exp.UnixToTime.from_arg_list(args)

    # https://www.postgresql.org/docs/current/functions-formatting.html
    return build_formatted_time(exp.StrToTime, "postgres")(args)


def _json_extract_sql(
    name: str, op: str
) -> t.Callable[[Postgres.Generator, JSON_EXTRACT_TYPE], str]:
    def _generate(self: Postgres.Generator, expression: JSON_EXTRACT_TYPE) -> str:
        if expression.args.get("only_json_types"):
            return json_extract_segments(name, quoted_index=False, op=op)(self, expression)
        return json_extract_segments(name)(self, expression)

    return _generate


class Postgres(Dialect):
    INDEX_OFFSET = 1
    TYPED_DIVISION = True
    CONCAT_COALESCE = True
    NULL_ORDERING = "nulls_are_large"
    TIME_FORMAT = "'YYYY-MM-DD HH24:MI:SS'"

    TIME_MAPPING = {
        "AM": "%p",
        "PM": "%p",
        "D": "%u",  # 1-based day of week
        "DD": "%d",  # day of month
        "DDD": "%j",  # zero padded day of year
        "FMDD": "%-d",  # - is no leading zero for Python; same for FM in postgres
        "FMDDD": "%-j",  # day of year
        "FMHH12": "%-I",  # 9
        "FMHH24": "%-H",  # 9
        "FMMI": "%-M",  # Minute
        "FMMM": "%-m",  # 1
        "FMSS": "%-S",  # Second
        "HH12": "%I",  # 09
        "HH24": "%H",  # 09
        "MI": "%M",  # zero padded minute
        "MM": "%m",  # 01
        "OF": "%z",  # utc offset
        "SS": "%S",  # zero padded second
        "TMDay": "%A",  # TM is locale dependent
        "TMDy": "%a",
        "TMMon": "%b",  # Sep
        "TMMonth": "%B",  # September
        "TZ": "%Z",  # uppercase timezone name
        "US": "%f",  # zero padded microsecond
        "WW": "%U",  # 1-based week of year
        "YY": "%y",  # 15
        "YYYY": "%Y",  # 2015
    }

    class Tokenizer(tokens.Tokenizer):
        BIT_STRINGS = [("b'", "'"), ("B'", "'")]
        HEX_STRINGS = [("x'", "'"), ("X'", "'")]
        BYTE_STRINGS = [("e'", "'"), ("E'", "'")]
        HEREDOC_STRINGS = ["$"]

        HEREDOC_TAG_IS_IDENTIFIER = True
        HEREDOC_STRING_ALTERNATIVE = TokenType.PARAMETER

        KEYWORDS = {
            **tokens.Tokenizer.KEYWORDS,
            "~~": TokenType.LIKE,
            "~~*": TokenType.ILIKE,
            "~*": TokenType.IRLIKE,
            "~": TokenType.RLIKE,
            "@@": TokenType.DAT,
            "@>": TokenType.AT_GT,
            "<@": TokenType.LT_AT,
            "|/": TokenType.PIPE_SLASH,
            "||/": TokenType.DPIPE_SLASH,
            "BEGIN": TokenType.COMMAND,
            "BEGIN TRANSACTION": TokenType.BEGIN,
            "BIGSERIAL": TokenType.BIGSERIAL,
            "CHARACTER VARYING": TokenType.VARCHAR,
            "CONSTRAINT TRIGGER": TokenType.COMMAND,
            "DECLARE": TokenType.COMMAND,
            "DO": TokenType.COMMAND,
            "EXEC": TokenType.COMMAND,
            "HSTORE": TokenType.HSTORE,
            "JSONB": TokenType.JSONB,
            "MONEY": TokenType.MONEY,
            "REFRESH": TokenType.COMMAND,
            "REINDEX": TokenType.COMMAND,
            "RESET": TokenType.COMMAND,
            "REVOKE": TokenType.COMMAND,
            "SERIAL": TokenType.SERIAL,
            "SMALLSERIAL": TokenType.SMALLSERIAL,
            "TEMP": TokenType.TEMPORARY,
            "CSTRING": TokenType.PSEUDO_TYPE,
            "OID": TokenType.OBJECT_IDENTIFIER,
            "OPERATOR": TokenType.OPERATOR,
            "REGCLASS": TokenType.OBJECT_IDENTIFIER,
            "REGCOLLATION": TokenType.OBJECT_IDENTIFIER,
            "REGCONFIG": TokenType.OBJECT_IDENTIFIER,
            "REGDICTIONARY": TokenType.OBJECT_IDENTIFIER,
            "REGNAMESPACE": TokenType.OBJECT_IDENTIFIER,
            "REGOPER": TokenType.OBJECT_IDENTIFIER,
            "REGOPERATOR": TokenType.OBJECT_IDENTIFIER,
            "REGPROC": TokenType.OBJECT_IDENTIFIER,
            "REGPROCEDURE": TokenType.OBJECT_IDENTIFIER,
            "REGROLE": TokenType.OBJECT_IDENTIFIER,
            "REGTYPE": TokenType.OBJECT_IDENTIFIER,
        }

        SINGLE_TOKENS = {
            **tokens.Tokenizer.SINGLE_TOKENS,
            "$": TokenType.HEREDOC_STRING,
        }

        VAR_SINGLE_TOKENS = {"$"}

    class Parser(parser.Parser):
        PROPERTY_PARSERS = {
            **parser.Parser.PROPERTY_PARSERS,
            "SET": lambda self: self.expression(exp.SetConfigProperty, this=self._parse_set()),
        }
        PROPERTY_PARSERS.pop("INPUT")

        FUNCTIONS = {
            **parser.Parser.FUNCTIONS,
            "DATE_TRUNC": build_timestamp_trunc,
            "GENERATE_SERIES": _build_generate_series,
            "JSON_EXTRACT_PATH": build_json_extract_path(exp.JSONExtract),
            "JSON_EXTRACT_PATH_TEXT": build_json_extract_path(exp.JSONExtractScalar),
            "MAKE_TIME": exp.TimeFromParts.from_arg_list,
            "MAKE_TIMESTAMP": exp.TimestampFromParts.from_arg_list,
            "NOW": exp.CurrentTimestamp.from_arg_list,
            "TO_CHAR": build_formatted_time(exp.TimeToStr, "postgres"),
            "TO_TIMESTAMP": _build_to_timestamp,
            "UNNEST": exp.Explode.from_arg_list,
        }

        FUNCTION_PARSERS = {
            **parser.Parser.FUNCTION_PARSERS,
            "DATE_PART": lambda self: self._parse_date_part(),
        }

        BITWISE = {
            **parser.Parser.BITWISE,
            TokenType.HASH: exp.BitwiseXor,
        }

        EXPONENT = {
            TokenType.CARET: exp.Pow,
        }

        RANGE_PARSERS = {
            **parser.Parser.RANGE_PARSERS,
            TokenType.AT_GT: binary_range_parser(exp.ArrayContains),
            TokenType.DAMP: binary_range_parser(exp.ArrayOverlaps),
            TokenType.DAT: lambda self, this: self.expression(
                exp.MatchAgainst, this=self._parse_bitwise(), expressions=[this]
            ),
            TokenType.LT_AT: binary_range_parser(exp.ArrayContained),
            TokenType.OPERATOR: lambda self, this: self._parse_operator(this),
        }

        STATEMENT_PARSERS = {
            **parser.Parser.STATEMENT_PARSERS,
            TokenType.END: lambda self: self._parse_commit_or_rollback(),
        }

        JSON_ARROWS_REQUIRE_JSON_TYPE = True

        def _parse_operator(self, this: t.Optional[exp.Expression]) -> t.Optional[exp.Expression]:
            while True:
                if not self._match(TokenType.L_PAREN):
                    break

                op = ""
                while self._curr and not self._match(TokenType.R_PAREN):
                    op += self._curr.text
                    self._advance()

                this = self.expression(
                    exp.Operator,
                    comments=self._prev_comments,
                    this=this,
                    operator=op,
                    expression=self._parse_bitwise(),
                )

                if not self._match(TokenType.OPERATOR):
                    break

            return this

        def _parse_date_part(self) -> exp.Expression:
            part = self._parse_type()
            self._match(TokenType.COMMA)
            value = self._parse_bitwise()

            if part and part.is_string:
                part = exp.var(part.name)

            return self.expression(exp.Extract, this=part, expression=value)

    class Generator(generator.Generator):
        SINGLE_STRING_INTERVAL = True
        RENAME_TABLE_WITH_DB = False
        LOCKING_READS_SUPPORTED = True
        JOIN_HINTS = False
        TABLE_HINTS = False
        QUERY_HINTS = False
        NVL2_SUPPORTED = False
        PARAMETER_TOKEN = "$"
        TABLESAMPLE_SIZE_IS_ROWS = False
        TABLESAMPLE_SEED_KEYWORD = "REPEATABLE"
        SUPPORTS_SELECT_INTO = True
        JSON_TYPE_REQUIRED_FOR_EXTRACTION = True
        SUPPORTS_UNLOGGED_TABLES = True
        LIKE_PROPERTY_INSIDE_SCHEMA = True
        MULTI_ARG_DISTINCT = False
        CAN_IMPLEMENT_ARRAY_ANY = True

        SUPPORTED_JSON_PATH_PARTS = {
            exp.JSONPathKey,
            exp.JSONPathRoot,
            exp.JSONPathSubscript,
        }

        TYPE_MAPPING = {
            **generator.Generator.TYPE_MAPPING,
            exp.DataType.Type.TINYINT: "SMALLINT",
            exp.DataType.Type.FLOAT: "REAL",
            exp.DataType.Type.DOUBLE: "DOUBLE PRECISION",
            exp.DataType.Type.BINARY: "BYTEA",
            exp.DataType.Type.VARBINARY: "BYTEA",
            exp.DataType.Type.DATETIME: "TIMESTAMP",
        }

        TRANSFORMS = {
            **generator.Generator.TRANSFORMS,
            exp.AnyValue: any_value_to_max_sql,
            exp.Array: lambda self, e: (
                f"{self.normalize_func('ARRAY')}({self.sql(e.expressions[0])})"
                if isinstance(seq_get(e.expressions, 0), exp.Select)
                else f"{self.normalize_func('ARRAY')}[{self.expressions(e, flat=True)}]"
            ),
            exp.ArrayConcat: rename_func("ARRAY_CAT"),
            exp.ArrayContained: lambda self, e: self.binary(e, "<@"),
            exp.ArrayContains: lambda self, e: self.binary(e, "@>"),
            exp.ArrayOverlaps: lambda self, e: self.binary(e, "&&"),
            exp.ArrayFilter: filter_array_using_unnest,
            exp.ArraySize: lambda self, e: self.func("ARRAY_LENGTH", e.this, e.expression or "1"),
            exp.BitwiseXor: lambda self, e: self.binary(e, "#"),
            exp.ColumnDef: transforms.preprocess([_auto_increment_to_serial, _serial_to_generated]),
            exp.CurrentDate: no_paren_current_date_sql,
            exp.CurrentTimestamp: lambda *_: "CURRENT_TIMESTAMP",
            exp.CurrentUser: lambda *_: "CURRENT_USER",
            exp.DateAdd: _date_add_sql("+"),
            exp.DateDiff: _date_diff_sql,
            exp.DateStrToDate: datestrtodate_sql,
            exp.DataType: _datatype_sql,
            exp.DateSub: _date_add_sql("-"),
            exp.Explode: rename_func("UNNEST"),
            exp.GroupConcat: _string_agg_sql,
            exp.JSONExtract: _json_extract_sql("JSON_EXTRACT_PATH", "->"),
            exp.JSONExtractScalar: _json_extract_sql("JSON_EXTRACT_PATH_TEXT", "->>"),
            exp.JSONBExtract: lambda self, e: self.binary(e, "#>"),
            exp.JSONBExtractScalar: lambda self, e: self.binary(e, "#>>"),
            exp.JSONBContains: lambda self, e: self.binary(e, "?"),
            exp.JSONPathKey: json_path_key_only_name,
            exp.JSONPathRoot: lambda *_: "",
            exp.JSONPathSubscript: lambda self, e: self.json_path_part(e.this),
            exp.LastDay: no_last_day_sql,
            exp.LogicalOr: rename_func("BOOL_OR"),
            exp.LogicalAnd: rename_func("BOOL_AND"),
            exp.Max: max_or_greatest,
            exp.MapFromEntries: no_map_from_entries_sql,
            exp.Min: min_or_least,
            exp.Merge: merge_without_target_sql,
            exp.PartitionedByProperty: lambda self, e: f"PARTITION BY {self.sql(e, 'this')}",
            exp.PercentileCont: transforms.preprocess(
                [transforms.add_within_group_for_percentiles]
            ),
            exp.PercentileDisc: transforms.preprocess(
                [transforms.add_within_group_for_percentiles]
            ),
            exp.Pivot: no_pivot_sql,
            exp.Pow: lambda self, e: self.binary(e, "^"),
            exp.Rand: rename_func("RANDOM"),
            exp.RegexpLike: lambda self, e: self.binary(e, "~"),
            exp.RegexpILike: lambda self, e: self.binary(e, "~*"),
            exp.Select: transforms.preprocess(
                [
                    transforms.eliminate_semi_and_anti_joins,
                    transforms.eliminate_qualify,
                ]
            ),
            exp.StrPosition: str_position_sql,
            exp.StrToTime: lambda self, e: self.func("TO_TIMESTAMP", e.this, self.format_time(e)),
            exp.StructExtract: struct_extract_sql,
            exp.Substring: _substring_sql,
            exp.TimeFromParts: rename_func("MAKE_TIME"),
            exp.TimestampFromParts: rename_func("MAKE_TIMESTAMP"),
            exp.TimestampTrunc: timestamptrunc_sql,
            exp.TimeStrToTime: timestrtotime_sql,
            exp.TimeToStr: lambda self, e: self.func("TO_CHAR", e.this, self.format_time(e)),
            exp.ToChar: lambda self, e: self.function_fallback_sql(e),
            exp.Trim: trim_sql,
            exp.TryCast: no_trycast_sql,
            exp.TsOrDsAdd: _date_add_sql("+"),
            exp.TsOrDsDiff: _date_diff_sql,
            exp.UnixToTime: lambda self, e: self.func("TO_TIMESTAMP", e.this),
            exp.VariancePop: rename_func("VAR_POP"),
            exp.Variance: rename_func("VAR_SAMP"),
            exp.Xor: bool_xor_sql,
        }

        PROPERTIES_LOCATION = {
            **generator.Generator.PROPERTIES_LOCATION,
            exp.PartitionedByProperty: exp.Properties.Location.POST_SCHEMA,
            exp.TransientProperty: exp.Properties.Location.UNSUPPORTED,
            exp.VolatileProperty: exp.Properties.Location.UNSUPPORTED,
        }

        def bracket_sql(self, expression: exp.Bracket) -> str:
            """Forms like ARRAY[1, 2, 3][3] aren't allowed; we need to wrap the ARRAY."""
            if isinstance(expression.this, exp.Array):
                expression.set("this", exp.paren(expression.this, copy=False))

            return super().bracket_sql(expression)

        def matchagainst_sql(self, expression: exp.MatchAgainst) -> str:
            this = self.sql(expression, "this")
            expressions = [f"{self.sql(e)} @@ {this}" for e in expression.expressions]
            sql = " OR ".join(expressions)
            return f"({sql})" if len(expressions) > 1 else sql
