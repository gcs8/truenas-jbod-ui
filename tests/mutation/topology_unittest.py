"""Run unittest normally, marking failures outside the test-method phase.

unittest reports per-test setup, teardown and cleanup errors under the test's
own name. Observe its method invocation rather than guessing from that name
or the exception text. Subtests report their result before the method exits.
"""
from __future__ import annotations

import sys
import unittest


class PhaseResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        invoke = test._callTestMethod
        self.body_errors = []
        self.in_body = False

        def method(*args, **kwargs):
            self.in_body = True
            try:
                return invoke(*args, **kwargs)
            except BaseException as error:
                self.body_errors.append(error)
                raise
            finally:
                self.in_body = False

        test._callTestMethod = method

    def _phase(self, err):
        if not getattr(self, "in_body", False) and not any(err[1] is error for error in getattr(self, "body_errors", ())):
            print("TOPOLOGY-FIXTURE-ERROR: failure outside test body", file=sys.stderr)

    def addFailure(self, test, err):
        self._phase(err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._phase(err)
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._phase(err)
        super().addSubTest(test, subtest, err)


class Runner(unittest.TextTestRunner):
    resultclass = PhaseResult


if __name__ == "__main__":
    unittest.main(module=None, testRunner=Runner)
