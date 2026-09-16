import argparse
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cli.enter_ai import Client, DEFAULT_API_URL, clean_url, normalize_global_args, parse_labels, task_payload


class EnterAICommandTests(unittest.TestCase):
    def test_cli_url_and_labels_helpers(self):
        self.assertEqual(clean_url("http://localhost:8000/api/"), DEFAULT_API_URL)
        self.assertEqual(parse_labels("launch, qa ,"), ["launch", "qa"])

    def test_create_task_payload_supports_subtasks(self):
        args = argparse.Namespace(
            title="Verify release",
            description=None,
            status="todo",
            priority="high",
            assignee_id=None,
            due_date=None,
            labels="qa, release",
            project_id="project-1",
            parent_id="task-1",
        )
        self.assertEqual(task_payload(args, creating=True), {
            "title": "Verify release",
            "description": None,
            "status": "todo",
            "priority": "high",
            "assignee_id": None,
            "due_date": None,
            "labels": ["qa", "release"],
            "project_id": "project-1",
            "parent_id": "task-1",
        })

    def test_global_flags_can_follow_subcommands(self):
        self.assertEqual(
            normalize_global_args(["tasks", "update", "task-1", "--status", "done", "--yes", "--json"]),
            ["--yes", "--json", "tasks", "update", "task-1", "--status", "done"],
        )

    def test_client_sends_token_and_decodes_json(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = Client(f"http://127.0.0.1:{server.server_port}/api", "sample-token").request("GET", "/dashboard")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(received, [("/api/dashboard", "Bearer sample-token")])
