// Cross-platform entry for `npm run build:test-profile`.
//
// npm runs package scripts through cmd.exe on Windows, where the POSIX
// `VITE_TESSALLITE_TEST_PROFILE=1 npm run build` prefix is not an assignment
// but a command name ("'VITE_TESSALLITE_TEST_PROFILE' is not recognized").
// Setting the flag on the child's environment here works on every shell.
import { spawnSync } from 'node:child_process';
import { TEST_PROFILE_FLAG } from './testProfileGuard.mjs';

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const result = spawnSync(npm, ['run', 'build'], {
  stdio: 'inherit',
  shell: process.platform === 'win32',
  env: { ...process.env, [TEST_PROFILE_FLAG]: '1' },
});
process.exit(result.status ?? 1);
