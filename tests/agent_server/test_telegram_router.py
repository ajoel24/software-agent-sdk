"""Tests for Telegram router."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from openhands.agent_server.api import create_app
from openhands.agent_server.config import Config
from openhands.agent_server.dependencies import get_conversation_service


@pytest.fixture
def client():
    config = Config(session_api_keys=[])
    app = create_app(config)
    app.dependency_overrides[get_conversation_service] = lambda: MagicMock()
    return TestClient(app)


def test_telegram_status_unconfigured(client):
    response = client.get("/telegram/status")
    assert response.status_code == 503


def test_telegram_start_missing_token(client):
    response = client.post("/telegram/start", json={})
    assert response.status_code == 400
    assert "bot_token" in response.json()["detail"]


def test_telegram_stop_unconfigured(client):
    response = client.post("/telegram/stop")
    assert response.status_code == 503


def test_telegram_chats_unconfigured(client):
    response = client.get("/telegram/chats")
    assert response.status_code == 503


def test_telegram_webhook_not_running(client):
    response = client.post("/telegram/webhook", json={"update_id": 1})
    assert response.status_code == 503
