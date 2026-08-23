/**
 * Bug-8184 — the client-side password rule must be the server's rule.
 *
 * The authority is `_validate_password` in
 * `tessallite/shared/schemas/domains/auth.py`, which every local-user create
 * and password-reset schema applies. Each case below is one of the four
 * `ValueError`s that validator raises, so a client rule that drifts from the
 * server — accepting what the API will reject, or refusing what it would take —
 * fails here rather than in a user's face.
 */
import { describe, it, expect } from "vitest";
import {
  PASSWORD_MIN_LENGTH,
  meetsPasswordPolicy,
  showsPasswordPolicyError,
} from "./passwordPolicy";

describe("password policy mirrors shared/schemas/domains/auth.py", () => {
  it("matches the server minimum length", () => {
    expect(PASSWORD_MIN_LENGTH).toBe(12);
  });

  it.each([
    ["Ab1defghijkl", "meets every rule"],
    ["A1bcdefghijk", "longer than the minimum"],
    ["P@ssw0rdXabc", "punctuation is allowed, not required"],
  ])("accepts %s (%s)", (password) => {
    expect(meetsPasswordPolicy(password)).toBe(true);
  });

  it.each([
    ["Ab1defgijkl", "Password must be at least 12 characters long"],
    ["ab1defgh", "Password must contain at least one uppercase letter"],
    ["AB1DEFGH", "Password must contain at least one lowercase letter"],
    ["Abcdefgh", "Password must contain at least one digit"],
    ["", "empty"],
  ])("rejects %s — %s", (password) => {
    expect(meetsPasswordPolicy(password)).toBe(false);
  });

  it("does not mark an untouched field as wrong", () => {
    // An empty box is "not filled in", not "invalid"; the submit button is
    // what refuses it. Flagging it red on open is noise.
    expect(showsPasswordPolicyError("")).toBe(false);
    expect(showsPasswordPolicyError("short")).toBe(true);
    expect(showsPasswordPolicyError("Ab1defghijkl")).toBe(false);
  });
});
