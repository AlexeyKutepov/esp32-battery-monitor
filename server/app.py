from __future__ import annotations

import json
import socket
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "battery_monitor.db"
STATIC_DIR = BASE_DIR / "static"
UDP_DISCOVERY_PORT = 4210
HTTP_PORT = 8080
DISCOVERY_TOKEN = "discover"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException) -> Any:
    return jsonify({"status": "error", "message": error.description}), error.code


@dataclass(slots=True)
class DeviceReport:
    device_id: str
    device_name: str
    voltage: float
    wifi_rssi: int | None
    firmware_version: str | None
    boot_count: int | None
    uptime_ms: int | None
    ip_address: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                desired_name TEXT,
                last_voltage REAL,
                last_seen TEXT,
                wifi_rssi INTEGER,
                firmware_version TEXT,
                boot_count INTEGER,
                uptime_ms INTEGER,
                ip_address TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                voltage REAL NOT NULL,
                seen_at TEXT NOT NULL,
                wifi_rssi INTEGER,
                FOREIGN KEY(device_id) REFERENCES devices(device_id)
            );
            """
        )


def validate_report(payload: dict[str, Any]) -> DeviceReport:
    device_id = str(payload.get("device_id", "")).strip()
    device_name = str(payload.get("device_name", "")).strip()

    if not device_id:
        raise BadRequest("device_id is required")
    if not device_name:
        raise BadRequest("device_name is required")

    try:
        voltage = float(payload["voltage"])
    except KeyError as exc:
        raise BadRequest("voltage is required") from exc
    except (TypeError, ValueError) as exc:
        raise BadRequest("voltage must be numeric") from exc

    return DeviceReport(
        device_id=device_id,
        device_name=device_name,
        voltage=voltage,
        wifi_rssi=int(payload["wifi_rssi"]) if payload.get("wifi_rssi") is not None else None,
        firmware_version=str(payload["firmware_version"]).strip() if payload.get("firmware_version") else None,
        boot_count=int(payload["boot_count"]) if payload.get("boot_count") is not None else None,
        uptime_ms=int(payload["uptime_ms"]) if payload.get("uptime_ms") is not None else None,
        ip_address=str(payload["ip_address"]).strip() if payload.get("ip_address") else None,
    )


def upsert_device(report: DeviceReport) -> str:
    timestamp = utc_now()
    with get_db() as connection:
        current = connection.execute(
            "SELECT desired_name FROM devices WHERE device_id = ?", (report.device_id,)
        ).fetchone()
        desired_name = current["desired_name"] if current and current["desired_name"] else report.device_name
        effective_name = desired_name or report.device_name

        connection.execute(
            """
            INSERT INTO devices (
                device_id, device_name, desired_name, last_voltage, last_seen,
                wifi_rssi, firmware_version, boot_count, uptime_ms, ip_address,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                device_name = excluded.device_name,
                desired_name = COALESCE(devices.desired_name, excluded.desired_name),
                last_voltage = excluded.last_voltage,
                last_seen = excluded.last_seen,
                wifi_rssi = excluded.wifi_rssi,
                firmware_version = excluded.firmware_version,
                boot_count = excluded.boot_count,
                uptime_ms = excluded.uptime_ms,
                ip_address = excluded.ip_address,
                updated_at = excluded.updated_at
            """,
            (
                report.device_id,
                report.device_name,
                report.device_name,
                report.voltage,
                timestamp,
                report.wifi_rssi,
                report.firmware_version,
                report.boot_count,
                report.uptime_ms,
                report.ip_address,
                timestamp,
                timestamp,
            ),
        )

        connection.execute(
            "INSERT INTO measurements (device_id, voltage, seen_at, wifi_rssi) VALUES (?, ?, ?, ?)",
            (report.device_id, report.voltage, timestamp, report.wifi_rssi),
        )

        connection.execute(
            "UPDATE devices SET last_voltage = ?, last_seen = ?, updated_at = ? WHERE device_id = ?",
            (report.voltage, timestamp, timestamp, report.device_id),
        )

    return effective_name


def list_devices() -> list[dict[str, Any]]:
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT
                device_id,
                COALESCE(desired_name, device_name) AS display_name,
                device_name,
                desired_name,
                last_voltage,
                last_seen,
                wifi_rssi,
                firmware_version,
                boot_count,
                uptime_ms,
                ip_address,
                created_at,
                updated_at
            FROM devices
            ORDER BY COALESCE(last_seen, created_at) DESC, device_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.route("/")
def index() -> Any:
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/health")
def health() -> Any:
    return jsonify({"status": "ok", "timestamp": utc_now()})


@app.route("/api/devices", methods=["GET"])
def get_devices() -> Any:
    return jsonify({"devices": list_devices(), "server_time": utc_now()})


@app.route("/api/devices/report", methods=["POST"])
def report_device() -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequest("JSON body is required")

    report = validate_report(payload)
    assigned_name = upsert_device(report)
    return jsonify({"status": "ok", "assigned_name": assigned_name, "server_time": utc_now()})


@app.route("/api/devices/<device_id>", methods=["PATCH"])
def rename_device(device_id: str) -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequest("JSON body is required")

    new_name = str(payload.get("device_name", "")).strip()
    if not new_name:
        raise BadRequest("device_name is required")

    with get_db() as connection:
        cursor = connection.execute(
            "UPDATE devices SET desired_name = ?, updated_at = ? WHERE device_id = ?",
            (new_name, utc_now(), device_id),
        )
        if cursor.rowcount == 0:
            raise BadRequest(f"Unknown device_id: {device_id}")

    return jsonify({"status": "ok", "device_id": device_id, "assigned_name": new_name})


@app.route("/static/<path:path>")
def static_files(path: str) -> Any:
    return send_from_directory(STATIC_DIR, path)


class DiscoveryServer(threading.Thread):
    def __init__(self, http_port: int) -> None:
        super().__init__(daemon=True)
        self.http_port = http_port
        self._stop_event = threading.Event()

    def run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", UDP_DISCOVERY_PORT))

            while not self._stop_event.is_set():
                try:
                    data, address = sock.recvfrom(2048)
                    payload = json.loads(data.decode("utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue

                if payload.get("type") != DISCOVERY_TOKEN:
                    continue

                response = json.dumps(
                    {
                        "type": "server_announce",
                        "http_port": self.http_port,
                        "server_time": utc_now(),
                    }
                ).encode("utf-8")
                sock.sendto(response, address)

    def stop(self) -> None:
        self._stop_event.set()


if __name__ == "__main__":
    init_db()
    discovery_server = DiscoveryServer(http_port=HTTP_PORT)
    discovery_server.start()
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False)
