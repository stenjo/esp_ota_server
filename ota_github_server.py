# File: ota_github_server.py

import base64
import os
import zipfile
import io
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
from functools import partial
import json
import requests  # type: ignore
from packaging.version import parse as parse_version  # ✅ for version sorting

CREDS_FILE = ".ota_credentials" if os.path.exists(".ota_credentials") \
    else os.path.expanduser("~/.ota_credentials")
PROJECTS_FILE = ".ota_projects.json" if os.path.exists(".ota_projects.json") \
    else os.path.expanduser("~/.ota_projects.json")

USERNAME = PASSWORD = ""
if os.path.exists(CREDS_FILE):
    with open(CREDS_FILE, encoding="utf-8") as f:
        line = f.readline().strip()
        if ";" in line:
            USERNAME, PASSWORD = line.split(";", 1)
AUTH_KEY = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

OTA_DIR = os.path.abspath("ota_files")
SYNC_INTERVAL = 3600

PROJECTS = {}
if os.path.exists(PROJECTS_FILE):
    with open(PROJECTS_FILE, encoding="utf-8") as pf:
        PROJECTS = json.load(pf)

class AuthHandler(SimpleHTTPRequestHandler):
    def do_auth_head(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="OTA Server"')
        self.end_headers()

    def is_authenticated(self):
        auth_header = self.headers.get('Authorization')
        return auth_header == f"Basic {AUTH_KEY}"

    def list_directory(self, path):
        project = os.path.basename(path.rstrip('/'))
        if project in PROJECTS:
            versions = sorted([
                d for d in os.listdir(path)
                if os.path.isdir(os.path.join(path, d))
            ], key=parse_version, reverse=True)

            version_file_path = os.path.join(path, "version")
            current_version = None
            if os.path.exists(version_file_path):
                with open(version_file_path, "r", encoding="utf-8") as vf:
                    current_version = vf.read().strip()

            html = f"<html><head><title>{project} - OTA</title></head><body>"
            html += f"<h1>{project} - Synced Versions</h1>"
            if current_version:
                html += f"<p><strong>Current Version:</strong> {current_version}</p>"
            html += f"<p><a href='/sync_now?project={project}'>Sync Now</a> | "
            html += f"<a href='/rollback?project={project}'>Rollback</a> | "
            html += "<a href='/'>Back to projects</a></p>"
            html += "<ul>"
            for ver in versions:
                html += f'<li><a href="{ver}/">{ver}</a>'
                py_files = [f for f in os.listdir(os.path.join(path, ver)) if f.endswith(".py")]
                if py_files:
                    html += "<ul>"
                    for f in py_files:
                        html += f'<li><a href="{ver}/{f}">{f}</a></li>'
                    html += "</ul>"
                html += "</li>"
            html += "</ul></body></html>"
            encoded = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return None

        return super().list_directory(path)

    def do_GET(self):
        if not self.is_authenticated():
            self.do_auth_head()
            self.wfile.write(b"Authentication required.")
            return

        parsed_path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_path.query)

        if parsed_path.path == "/set_version":
            new_version = query.get("version", [None])[0]
            project = query.get("project", [None])[0]
            if new_version and project in PROJECTS:
                version_file = os.path.join(OTA_DIR, project, "version")
                with open(version_file, "w", encoding="utf-8") as vf:
                    vf.write(new_version)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"Version for {project} set to {new_version}".encode())
                return
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing or invalid parameters.")
                return

        if parsed_path.path == "/sync_now":
            project = query.get("project", [None])[0]
            if project in PROJECTS:
                try:
                    repo = PROJECTS[project]
                    tags = get_latest_tags(repo, count=2)
                    tags = sorted(tags, key=parse_version, reverse=True)
                    for tag in tags:
                        fetch_github_release(tag, project, repo)
                    version_file = os.path.join(OTA_DIR, project, "version")
                    with open(version_file, "w", encoding="utf-8") as vf:
                        vf.write(tags[0])
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(f"Manually synced {project} tags: {', '.join(tags)}".encode())
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode())
                return

        if parsed_path.path == "/rollback":
            project = query.get("project", [None])[0]
            if project in PROJECTS:
                project_path = os.path.join(OTA_DIR, project)
                versions = sorted([
                    d for d in os.listdir(project_path)
                    if os.path.isdir(os.path.join(project_path, d))
                ], key=parse_version, reverse=True)
                version_file = os.path.join(project_path, "version")
                if len(versions) >= 2:
                    with open(version_file, "w", encoding="utf-8") as vf:
                        vf.write(versions[1])
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(f"Rolled back {project} to {versions[1]}".encode())
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"No previous version to roll back to.")
                return

        if parsed_path.path == "/set_latest":
            project = query.get("project", [None])[0]
            if project in PROJECTS:
                latest_file = os.path.join(OTA_DIR, project, "latest")
                version_file = os.path.join(OTA_DIR, project, "version")
                if os.path.exists(latest_file):
                    with open(latest_file, "r") as lf:
                        latest_version = lf.read().strip()
                    with open(version_file, "w") as vf:
                        vf.write(latest_version)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(f"Set {project} version back to latest ({latest_version})".encode())
                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"No latest version file found.")
                return

        return super().do_GET()

def get_latest_tags(repo, count=2):
    url = f"https://api.github.com/repos/{repo}/tags"
    r = requests.get(url)
    r.raise_for_status()
    tags = r.json()
    return [tag["name"] for tag in tags[:count]] if tags else []

def fetch_github_release(tag: str, project: str, repo: str):
    release_path = os.path.join(OTA_DIR, project, tag)
    if os.path.exists(release_path) and os.listdir(release_path):
        print(f"[{project}] Tag {tag} already synced.")
        return

    os.makedirs(release_path, exist_ok=True)
    zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
    print(f"[{project}] Fetching zip: {zip_url}")
    zip_res = requests.get(zip_url)
    zip_res.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
        for zip_info in z.infolist():
            if zip_info.filename.endswith(".py") and "/src/" in zip_info.filename:
                target_name = os.path.basename(zip_info.filename)
                with open(os.path.join(release_path, target_name), 'wb') as f:
                    f.write(z.read(zip_info))

    latest_file = os.path.join(OTA_DIR, project, "latest")
    with open(latest_file, "w", encoding="utf-8") as lf:
        lf.write(tag)

    print(f"[{project}] Fetched release {tag} to {release_path}")

def sync_latest_releases():
    for project, repo in PROJECTS.items():
        try:
            tags = get_latest_tags(repo, count=2)
            if not tags:
                continue
            tags = sorted(tags, key=parse_version, reverse=True)
            latest_tag = tags[0]
            version_file = os.path.join(OTA_DIR, project, "version")
            with open(version_file, "w", encoding="utf-8") as vf:
                vf.write(latest_tag)
            for tag in tags:
                fetch_github_release(tag, project, repo)
        except Exception as e:
            print(f"[{project}] Sync error: {e}")

def periodic_sync():
    while True:
        sync_latest_releases()
        time.sleep(SYNC_INTERVAL)

if __name__ == '__main__':
    threading.Thread(target=periodic_sync, daemon=True).start()
    handler = partial(AuthHandler, directory=OTA_DIR)
    server = HTTPServer(('0.0.0.0', 8000), handler)
    print("OTA Server running at http://0.0.0.0:8000")
    server.serve_forever()
