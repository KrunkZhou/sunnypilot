import ast
from abc import ABC, abstractmethod
from pathlib import Path
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class ProcessTests(unittest.TestCase):
  def setUp(self):
    # Exercise the actual generic start/stop methods without platform-native
    # messaging imports or spawning receiver/radio processes on the host.
    self.now = 0.
    self.children = []
    self.fail_start = False
    test = self
    class Child:
      def __init__(self, **kwargs):
        self.exitcode = None
        self.pid = 100 + len(test.children)
        self.joined = False
        test.children.append(self)
      def start(self):
        if test.fail_start:
          raise OSError('simulated spawn failure')
      def join(self):
        self.joined = True
      def is_alive(self):
        return self.exitcode is None
    def kill(pid, sig):
      next(child for child in self.children if child.pid == pid).exitcode = -sig
    namespace = {'ABC': ABC, 'abstractmethod': abstractmethod, 'Process': Child,
                 'cloudlog': Mock(), 'launcher': lambda *args: None, 'signal': signal,
                 'time': SimpleNamespace(monotonic=lambda: self.now), 'os': SimpleNamespace(kill=kill),
                 'join_process': lambda child, timeout: child.join()}
    root = Path(__file__).resolve().parents[3]
    sources = ((root / 'system/manager/process.py', ('ManagerProcess', 'PythonProcess')),
               (root / 'sunnypilot/external_navigation/process.py', ('ExternalNavigationProcess',)))
    for path, names in sources:
      nodes = [node for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef) and node.name in names]
      future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
      module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
      exec(compile(module, str(path), 'exec'), namespace)
    self.generic = namespace['PythonProcess']
    self.process = namespace['ExternalNavigationProcess']('external_navigationd', 'receiver', lambda *args: True)

  def test_running_child_is_not_duplicated(self):
    self.process.start()
    self.now = 100
    self.process.start()
    self.assertEqual(len(self.children), 1)

  def test_crashes_are_reaped_with_bounded_backoff(self):
    self.process.start()
    for delay in (1, 2, 4, 8, 16, 30, 30):
      child = self.process.proc
      child.exitcode = 1
      self.process.start()
      self.assertTrue(child.joined)
      self.assertIsNone(self.process.proc)
      count = len(self.children)
      self.now += delay - .01
      self.process.start()
      self.assertEqual(len(self.children), count)
      self.now += .01
      self.process.start()
      self.assertEqual(len(self.children), count + 1)

  def test_stable_run_resets_backoff(self):
    self.process.start()
    self.process.restart_delay = 30
    self.now = 60
    self.process.proc.exitcode = 1
    self.process.start()
    self.assertEqual(self.process.restart_at, 61)
    self.assertEqual(self.process.restart_delay, 2)

  def test_intentional_stop_cancels_pending_retry(self):
    self.process.start()
    self.process.proc.exitcode = 1
    self.process.start()
    self.assertEqual(self.process.restart_at, 1)
    self.process.stop(block=False)
    self.assertEqual(self.process.restart_at, 0)
    self.process.start()
    self.assertEqual(len(self.children), 2)
    self.assertEqual(self.process.restart_delay, 1)

  def test_reenable_finishes_nonblocking_shutdown(self):
    self.process.start()
    child = self.process.proc
    self.process.stop(block=False)
    self.assertTrue(self.process.shutting_down)
    self.assertEqual(child.exitcode, -signal.SIGINT)
    self.process.start()
    self.assertIsNot(self.process.proc, child)
    self.assertEqual(self.process.restart_at, 0)
    self.assertFalse(self.process.shutting_down)
    self.process.stop()
    self.assertIsNone(self.process.proc)

  def test_spawn_failure_has_backoff_and_no_poisoned_handle(self):
    self.fail_start = True
    self.process.start()
    self.assertIsNone(self.process.proc)
    self.assertEqual(self.process.restart_at, 1)
    self.fail_start = False
    self.now = 1
    self.process.start()
    self.assertIsNotNone(self.process.proc)

  def test_generic_process_behavior_and_registration_are_unchanged(self):
    generic = self.generic('other', 'module', lambda *args: True)
    generic.start()
    child = generic.proc
    child.exitcode = 1
    generic.start()
    self.assertIs(generic.proc, child)
    path = Path(__file__).resolve().parents[3] / 'system/manager/process_config.py'
    calls = [node for node in ast.walk(ast.parse(path.read_text()))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'ExternalNavigationProcess']
    self.assertEqual(len(calls), 1)
    self.assertEqual(ast.literal_eval(calls[0].args[0]), 'external_navigationd')


if __name__ == '__main__':
  unittest.main()
