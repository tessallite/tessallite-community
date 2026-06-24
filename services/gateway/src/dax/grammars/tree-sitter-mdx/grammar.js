/**
 * Tree-sitter grammar for MDX (Multidimensional Expressions).
 *
 * Structural parser — captures WITH MEMBER/SET, SELECT ... ON axis,
 * FROM [cube], WHERE (tuple), and DIMENSION PROPERTIES.
 * Axis body expressions are captured as raw text for downstream regex
 * helpers that already handle drill-down, hierarchize, etc.
 */
module.exports = grammar({
  name: "mdx",

  extras: $ => [/\s+/],

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
      field("name", $.dotted_ref),
      ci("AS"),
      field("expression", $.calc_expression),
      repeat(seq(",", $.member_property)),
    ),

    with_set_def: $ => seq(
      ci("SET"),
      field("name", $.dotted_ref),
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

    calc_atom: $ => choice(
      $.dotted_ref,
      $.number,
      $.string_literal,
      $.identifier,
      $.calc_paren,
      $.operator,
    ),

    calc_paren: $ => seq("(", repeat($.calc_atom), ")"),

    operator: $ => choice("+", "-", "*", "/", "=", "<>", "<", ">", "<=", ">=", ","),

    // ---- SELECT statement ----

    select_statement: $ => seq(
      ci("SELECT"),
      optional($.axis_list),
      ci("FROM"),
      $.from_clause,
      optional($.where_clause),
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

    axis_token: $ => choice(
      $.dotted_ref,
      $.set_literal,
      $.func_call,
      $.number,
      $.string_literal,
      $.identifier,
    ),

    func_call: $ => seq(
      $.identifier,
      "(",
      repeat(choice(
        $.axis_token,
        ",",
      )),
      ")",
    ),

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

    where_tuple: $ => seq(
      "(",
      repeat1(choice(
        $.dotted_ref,
        $.set_literal,
        ",",
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

    number: $ => /-?\d+(\.\d+)?/,

    string_literal: $ => /"[^"]*"/,
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
