/**
 * The password rule the backend actually enforces, stated once for the UI.
 *
 * Mirrors `_validate_password` in `shared/schemas/domains/auth.py`: at least 12
 * characters, at least one uppercase letter, one lowercase letter and one
 * digit. Every local-user create and password-reset endpoint applies it
 * (`UserCreate`, `UserPasswordReset`), and no form said so — an operator typed
 * a password, submitted, and learned the rule from a 422.
 *
 * This is the SPA half of the defect Bug-8184 names. That issue's own evidence
 * is the two bootstrap scripts (`deploy/local/steps/05_post_deploy.sh`,
 * `deploy/gcp/steps/08_post_deploy.sh`), which still prompt "min 8 chars" and
 * are NOT touched here; the issue stays open for them.
 *
 * This does NOT replace the server check; it is the same rule said earlier, so
 * the operator reads it before typing rather than after failing. The server
 * stays the authority, which is why this file holds no error message of its
 * own — the wording lives in `errors.form.passwordComplexity`.
 *
 * One module rather than a copy per form: the rule is duplicated in three
 * password forms (tenant users & access, system admin, tenant admin), and three
 * hand-written regex triples drift.
 */
export const PASSWORD_MIN_LENGTH = 12;

export function meetsPasswordPolicy(password: string): boolean {
  return (
    password.length >= PASSWORD_MIN_LENGTH &&
    /[A-Z]/.test(password) &&
    /[a-z]/.test(password) &&
    /[0-9]/.test(password)
  );
}

/**
 * True when the field should be shown as invalid.
 *
 * An empty field is "not filled in yet", not "wrong" — flagging it red the
 * moment the dialog opens is noise, and the submit button already refuses.
 */
export function showsPasswordPolicyError(password: string): boolean {
  return password.length > 0 && !meetsPasswordPolicy(password);
}
