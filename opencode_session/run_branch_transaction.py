import os
import subprocess


class BranchDeleteTransaction:
    def __init__(self, process):
        self.process = process

    @classmethod
    def prepare(cls, git_dir, branch, expected_tip):
        ref_name = f"refs/heads/{branch}"
        valid = subprocess.run(
            ["git", "check-ref-format", ref_name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if valid.returncode != 0:
            return None, _error(valid, "recorded branch name is invalid")
        try:
            process = subprocess.Popen(
                ["git", "--git-dir", git_dir, "update-ref", "--stdin"],
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "GIT_FLUSH": "1"},
            )
            process.stdin.write(f"start\ndelete {ref_name} {expected_tip}\nprepare\n")
            process.stdin.flush()
            responses = (process.stdout.readline().strip(), process.stdout.readline().strip())
        except OSError as error:
            return None, f"cannot prepare atomic branch deletion: {error}"
        transaction = cls(process)
        if responses != ("start: ok", "prepare: ok"):
            return None, transaction._finish("abort", fallback="branch changed before deletion")
        return transaction, None

    def commit(self):
        return self._finish("commit", fallback="atomic branch deletion failed")

    def abort(self):
        return self._finish("abort", fallback="atomic branch deletion abort failed")

    def _finish(self, command, *, fallback):
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            self.process.stdin.close()
            stdout = self.process.stdout.read().strip()
            stderr = self.process.stderr.read().strip()
            return_code = self.process.wait()
            self.process.stdout.close()
            self.process.stderr.close()
        except (OSError, ValueError) as error:
            self.process.kill()
            self.process.wait()
            return f"{fallback}: {error}"
        expected = f"{command}: ok"
        if return_code == 0 and expected in stdout.splitlines():
            return None
        return stderr or stdout or fallback


def _error(completed, fallback):
    return completed.stderr.strip() or completed.stdout.strip() or fallback
