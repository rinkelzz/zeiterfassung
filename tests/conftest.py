"""Gemeinsame Testumgebung – wird vor allen Testdateien geladen."""
import os
import tempfile

os.environ["ZE_DATA_DIR"] = tempfile.mkdtemp()
os.environ["ZE_ADMIN_PASSWORD"] = "admin-passwort"
os.environ["ZE_PUBLIC_URL"] = "https://zeit.example.de"
os.environ["ZE_MAIL_SCHEDULER"] = "0"
os.environ["ZE_UPDATE_CHECK"] = "0"
