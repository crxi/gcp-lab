"""Image source cleanup after uncertain operation outcomes; no API calls."""

import unittest
from unittest import mock
from test_llm_chat import load


class ImageCleanup(unittest.TestCase):
    def test_uncertain_image_creation_retains_source_but_success_cleans_up(self):
        import contextlib
        import tempfile
        builder = load("llm_chat_image_cleanup", "image.py")
        for failure in (None, TimeoutError("poll failed"), KeyboardInterrupt(),
                        SystemExit("operation timeout")):
            with self.subTest(failure=type(failure).__name__), \
                    tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
                for name, value in (("RESULTS", d),):
                    stack.enter_context(mock.patch.object(builder, name, value))
                for name in ("wait_running", "wait_ssh", "log"):
                    stack.enter_context(mock.patch.object(builder, name))
                stack.enter_context(mock.patch.object(builder, "create_gpu", return_value="z"))
                for name in ("scp_to", "ssh"):
                    stack.enter_context(mock.patch.object(builder, name, return_value=mock.Mock(returncode=0)))
                stack.enter_context(mock.patch.object(builder, "stream", return_value=(0, "")))
                cleanup = stack.enter_context(mock.patch.object(builder, "delete_instances"))
                for name in ("cmd_stop", "write_config"):
                    stack.enter_context(mock.patch.object(builder.glab, name))
                stack.enter_context(mock.patch.object(builder.glab, "cmd_image_create", side_effect=failure))
                if failure is None:
                    builder.build("z")
                    cleanup.assert_called_once()
                else:
                    with self.assertRaises(type(failure)):
                        builder.build("z")
                    cleanup.assert_not_called()
