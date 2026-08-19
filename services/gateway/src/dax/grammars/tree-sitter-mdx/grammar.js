/**
 * Tree-sitter grammar for MDX (Multidimensional Expressions).
 *
 * Structural parser — captures WITH MEMBER/SET, SELECT ... ON axis,
 * FROM [cube], WHERE (tuple), and DIMENSION PROPERTIES.
 * Axis and calc-body expressions are captured as raw text for downstream regex
 * helpers that already handle drill-down, hierarchize, etc. The grammar is
 * PERMISSIVE inside expressions (operators, parens, sets, function calls,
 * comments, single-quoted bodies) so VALID SSAS MDX never sets root.has_error —
 * the XMLA Execute admission gate (xmla_server._parse_mdx_for_execute) fails
 * closed on has_error, so a false positive would refuse a valid Excel/Power BI
 * query. The structural skeleton (SELECT/axis/ON/FROM cube/WHERE) stays strict
 * so genuinely malformed MDX still errors.
 *
 * REGENERATION (Wave C, Bug-9443): the committed src/parser.c and the two
 * mdx.so artifacts MUST stay ABI 14 to load with the runtime `tree_sitter`
 * 0.21.3. Regenerate with the pinned CLI and the explicit ABI flag:
 *
 *     cd tessallite/services/gateway/src/dax/grammars/tree-sitter-mdx
 *     npm install                              # tree-sitter-cli 0.26.8 (pinned)
 *     ./node_modules/.bin/tree-sitter generate --abi 14
 *     # then rebuild both mdx.so via tree_sitter.Language.build_library(...)
 *
 * The default (ABI 15) will NOT load with the 0.21.3 runtime.
 */
module.exports = grammar({
  name: "mdx",

  // Whitespace and comments are ignored between tokens. The committed grammar
  // had no comment rule, so ANY SSAS comment (// -- /* */) previously set
  // root.has_error on an otherwise valid statement.
  extras: $ => [/\s+/, $.comment],

  word: $ => $.identifier,

  conflicts: $ => [],

  rules: {
    source_file: $ => choice(
      $.drillthrough_statement,
      $.mdx_statement,
    ),

    mdx_statement: $ => seq(
      optional($.with_clause),
      $.select_statement,
    ),

    // ---- DRILLTHROUGH statement ----

    drillthrough_statement: $ => seq(
      ci("DRILLTHROUGH"),
      optional($.maxrows_clause),
      optional($.with_clause),
      $.select_statement,
      optional($.return_clause),
    ),

    maxrows_clause: $ => seq(
      ci("MAXROWS"),
      field("count", $.number),
    ),

    return_clause: $ => seq(
      ci("RETURN"),
      commaSep1($.return_column),
    ),

    return_column: $ => $.dotted_ref,

    // ---- WITH clause ----

    with_clause: $ => seq(
      ci("WITH"),
      repeat1(choice($.with_member_def, $.with_set_def)),
    ),

    with_member_def: $ => seq(
      ci("MEMBER"),
      field("name", choice($.dotted_ref, $.identifier)),
      ci("AS"),
      field("expression", $.calc_expression),
      repeat(seq(",", $.member_property)),
    ),

    // SSAS names a WITH SET / MEMBER with a bracketed name (``[FS]``) OR a bare
    // identifier (``WITH SET FilteredMembers AS ...``). Only the bracketed form
    // parsed before, so every bare-named set set has_error.
    with_set_def: $ => seq(
      ci("SET"),
      field("name", choice($.dotted_ref, $.identifier)),
      ci("AS"),
      field("expression", $.calc_expression),
    ),

    member_property: $ => seq(
      $.identifier,
      "=",
      choice($.string_literal, $.number, $.dotted_ref),
    ),

    // Calculated expression — captures arithmetic, function calls, member
    // refs, IIF, and nested parens. Kept flat to avoid conflicts with
    // axis_body.
    calc_expression: $ => prec.left(repeat1($.calc_atom)),

    // A WITH MEMBER / SET body atom. Includes sets ({...}) and function calls
    // (AGGREGATE(...), SUM(...), IIF(...)) so a calc member over a set —
    // ``AGGREGATE({[A],[B],[C]})`` — parses without has_error.
    calc_atom: $ => choice(
      $.dotted_ref,
      $.number,
      $.string_literal,
      $.identifier,
      $.set_literal,
      $.func_call,
      $.calc_paren,
      $.operator,
    ),

    calc_paren: $ => seq("(", repeat(choice($.calc_atom, ",", ":")), ")"),

    // Comma is a structural separator (sets, function args, member-property
    // list), never an expression operator — keeping it here made ``,`` ambiguous
    // once calc_paren accepted explicit commas.
    operator: $ => choice("+", "-", "*", "/", "=", "<>", "<", ">", "<=", ">="),

    // ---- SELECT statement ----

    select_statement: $ => seq(
      ci("SELECT"),
      optional($.axis_list),
      ci("FROM"),
      $.from_clause,
      optional($.where_clause),
      optional($.cell_properties),
    ),

    // Trailing ``CELL PROPERTIES <prop>, ...`` clause (Excel sends
    // ``CELL PROPERTIES CELL_ORDINAL, VALUE, FORMATTED_VALUE``). Ignored by the
    // walker, but it must be part of the grammar or it sets has_error.
    cell_properties: $ => seq(
      ci("CELL"),
      ci("PROPERTIES"),
      commaSep1(choice($.dotted_ref, $.identifier)),
    ),

    axis_list: $ => seq(
      $.axis_def,
      repeat(seq(",", $.axis_def)),
    ),

    axis_def: $ => seq(
      optional(seq(ci("NON"), ci("EMPTY"))),
      field("body", $.axis_body),
      optional($.dim_properties),
      ci("ON"),
      field("axis_name", $.axis_id),
    ),

    axis_id: $ => choice(ci("COLUMNS"), ci("ROWS"), /[01]/),

    // The axis body is everything between (NON EMPTY)? and (DIMENSION PROPERTIES | ON).
    // We capture it as a sequence of tokens so downstream regex helpers can process it.
    axis_body: $ => repeat1($.axis_token),

    // An axis body token. Includes operators and parenthesised groups so a
    // valid MDX predicate/expression on an axis — Filter(set, Left(x,2) = "US"),
    // a subselect tuple ({...}), Generate/Ascendants, etc. — parses without
    // has_error. The body is still captured as raw text for the regex helpers.
    axis_token: $ => choice(
      $.dotted_ref,
      $.set_literal,
      $.func_call,
      $.number,
      $.string_literal,
      $.identifier,
      $.paren_group,
      $.op,
      $.at_param,
    ),

    // Comparison / arithmetic operators usable inside an axis expression
    // (a boolean predicate such as ``Left(x,2) = "US"`` inside Filter()).
    // Comma stays a structural separator, not an operator, to avoid ambiguity.
    op: $ => choice("+", "-", "*", "/", "=", "<>", "<", ">", "<=", ">="),

    // A bare parenthesised group on an axis: a tuple, or the ``({...})`` that a
    // subselect axis wraps its member set in.
    paren_group: $ => seq(
      "(",
      repeat(choice(
        $.axis_token,
        ",",
        ":",
      )),
      ")",
    ),

    // prec(1): an identifier immediately followed by "(" is a function call,
    // not a bare identifier followed by a paren_group.
    func_call: $ => prec(1, seq(
      $.identifier,
      "(",
      repeat(choice(
        $.axis_token,
        ",",
        ":",
      )),
      ")",
    )),

    set_literal: $ => seq(
      "{",
      repeat(choice(
        $.axis_token,
        ",",
        ":",
      )),
      "}",
    ),

    dim_properties: $ => seq(
      ci("DIMENSION"),
      ci("PROPERTIES"),
      commaSep1(choice($.dotted_ref, $.identifier)),
    ),

    // ---- FROM clause ----

    from_clause: $ => choice(
      $.bracket_name,
      $.subselect,
    ),

    subselect: $ => seq(
      "(",
      $.select_statement,
      ")",
    ),

    // ---- WHERE clause ----

    where_clause: $ => seq(
      ci("WHERE"),
      $.where_tuple,
    ),

    // A WHERE slicer body. Permissive the SAME way axis bodies are — a slicer can
    // be a tuple, a set, a KPI/STRTOSET/STRTOMEMBER function call, an inline @param,
    // an arithmetic expression, or a nested-paren tuple. dotted_ref and set_literal
    // stay DIRECT children so the walker's where-member extraction keeps working
    // for the common single-member/set slicers; the extra shapes parse clean and
    // are read from raw text by the regex helpers. The outer ``(`` ... ``)`` with
    // repeat1 keeps the skeleton strict, so a malformed/empty slicer still errors.
    where_tuple: $ => seq(
      "(",
      repeat1(choice(
        $.dotted_ref,
        $.set_literal,
        $.func_call,
        $.paren_group,
        $.op,
        $.at_param,
        $.number,
        $.string_literal,
        ",",
        ":",
      )),
      ")",
    ),

    // ---- Shared primitives ----

    // [Name].[Name].[Name].&[Key] etc.
    // Composite key paths chain WITHOUT a dot between segments
    // (SSAS path-qualified unames, B8 round-3 Bug-1049):
    // [Name].[Name].&[Key0]&[Key1]
    dotted_ref: $ => seq(
      $.bracket_name,
      repeat(choice(
        seq(".", choice(
          $.bracket_name,
          $.ampersand_key,
          $.identifier,
        )),
        $.ampersand_key,
      )),
    ),

    ampersand_key: $ => seq("&", $.bracket_name),

    bracket_name: $ => /\[[^\]]*\]/,

    identifier: $ => /[A-Za-z_]\w*/,

    // SSAS inline parameter token: STRTOSET(@Region, CONSTRAINED),
    // STRTOMEMBER(@Year). Without this the ``@`` set has_error.
    at_param: $ => /@[A-Za-z_]\w*/,

    number: $ => /-?\d+(\.\d+)?/,

    // MDX uses BOTH double-quoted string literals and single-quoted calc /
    // named-set expression bodies (``AS '{...}'``). The committed grammar only
    // recognised double quotes, so every single-quoted WITH MEMBER/SET body set
    // has_error.
    string_literal: $ => token(choice(
      /"[^"]*"/,
      /'[^']*'/,
    )),

    // Line (// and --) and block (/* */) comments — all valid SSAS MDX.
    comment: $ => token(choice(
      seq("//", /[^\n]*/),
      seq("--", /[^\n]*/),
      seq("/*", /[^*]*\*+([^/*][^*]*\*+)*/, "/"),
    )),
  },
});

function ci(word) {
  return new RegExp(
    word.split("").map(c =>
      /[a-zA-Z]/.test(c) ? `[${c.toLowerCase()}${c.toUpperCase()}]` : c
    ).join("")
  );
}

function commaSep1(rule) {
  return seq(rule, repeat(seq(",", rule)));
}
