from __future__ import annotations

import json
import socket
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
DEFAULT_SLEEP_SECONDS = 300
MIN_SLEEP_SECONDS = 30
MAX_SLEEP_SECONDS = 86400
LOW_VOLTAGE_THRESHOLD = 11.0
OFFLINE_SLEEP_MULTIPLIER = 3

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


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    table_info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = {row["name"] for row in table_info}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with get_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                desired_name TEXT,
                desired_sleep_seconds INTEGER,
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

        ensure_column(connection, "devices", "desired_name", "TEXT")
        ensure_column(connection, "devices", "desired_sleep_seconds", "INTEGER")


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


def upsert_device(report: DeviceReport) -> dict[str, Any]:
    timestamp = utc_now()
    with get_db() as connection:
        current = connection.execute(
            "SELECT desired_name, desired_sleep_seconds FROM devices WHERE device_id = ?", (report.device_id,)
        ).fetchone()
        desired_name = current["desired_name"] if current and current["desired_name"] else report.device_name
        desired_sleep_seconds = (
            current["desired_sleep_seconds"]
            if current and current["desired_sleep_seconds"] is not None
            else DEFAULT_SLEEP_SECONDS
        )
        effective_name = desired_name or report.device_name

        connection.execute(
            """
            INSERT INTO devices (
                device_id, device_name, desired_name, desired_sleep_seconds, last_voltage, last_seen,
                wifi_rssi, firmware_version, boot_count, uptime_ms, ip_address,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                device_name = excluded.device_name,
                desired_name = COALESCE(devices.desired_name, excluded.desired_name),
                desired_sleep_seconds = COALESCE(devices.desired_sleep_seconds, excluded.desired_sleep_seconds),
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
                DEFAULT_SLEEP_SECONDS,
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

    return {
        "assigned_name": effective_name,
        "sleep_seconds": int(desired_sleep_seconds),
    }


def list_devices() -> list[dict[str, Any]]:
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT
                device_id,
                COALESCE(desired_name, device_name) AS display_name,
                device_name,
                desired_name,
                COALESCE(desired_sleep_seconds, ?) AS desired_sleep_seconds,
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
            """,
            (DEFAULT_SLEEP_SECONDS,),
        ).fetchall()

    devices: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        device = dict(row)
        voltage = device.get("last_voltage")
        sleep_seconds = int(device.get("desired_sleep_seconds") or DEFAULT_SLEEP_SECONDS)
        deadline = sleep_seconds * OFFLINE_SLEEP_MULTIPLIER
        last_seen_raw = device.get("last_seen")
        is_offline = False

        if last_seen_raw:
            try:
                last_seen_dt = datetime.fromisoformat(last_seen_raw)
                if last_seen_dt.tzinfo is None:
                    last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
                is_offline = now - last_seen_dt > timedelta(seconds=deadline)
            except ValueError:
                is_offline = False

        device["is_low_voltage"] = voltage is not None and float(voltage) < LOW_VOLTAGE_THRESHOLD
        device["is_offline"] = is_offline
        device["status_text"] = "Нет связи с устройством" if is_offline else "Онлайн"
        devices.append(device)
    return devices


def get_measurements(device_id: str, limit: int = 100) -> list[dict[str, Any]]:
    limited = max(10, min(1000, int(limit)))
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT voltage, seen_at, wifi_rssi
            FROM measurements
            WHERE device_id = ?
            ORDER BY seen_at DESC
            LIMIT ?
            """,
            (device_id, limited),
        ).fetchall()

    return [dict(row) for row in reversed(rows)]


def parse_sleep_seconds(payload: dict[str, Any]) -> int:
    try:
        value = int(payload.get("sleep_seconds"))
    except (TypeError, ValueError) as exc:
        raise BadRequest("sleep_seconds must be an integer") from exc

    if value < MIN_SLEEP_SECONDS or value > MAX_SLEEP_SECONDS:
        raise BadRequest(f"sleep_seconds must be between {MIN_SLEEP_SECONDS} and {MAX_SLEEP_SECONDS}")
    return value


@app.route("/")
def index() -> Any:
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/health")
def health() -> Any:
    return jsonify({"status": "ok", "timestamp": utc_now()})


@app.route("/api/devices", methods=["GET"])
def get_devices() -> Any:
    return jsonify({"devices": list_devices(), "server_time": utc_now(), "low_voltage_threshold": LOW_VOLTAGE_THRESHOLD})


@app.route("/api/devices/report", methods=["POST"])
def report_device() -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequest("JSON body is required")

    report = validate_report(payload)
    assignment = upsert_device(report)
    return jsonify(
        {
            "status": "ok",
            "assigned_name": assignment["assigned_name"],
            "sleep_seconds": assignment["sleep_seconds"],
            "server_time": utc_now(),
        }
    )


@app.route("/api/devices/<device_id>", methods=["PATCH"])
def update_device(device_id: str) -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequest("JSON body is required")

    updates: list[str] = []
    params: list[Any] = []

    if "device_name" in payload:
        new_name = str(payload.get("device_name", "")).strip()
        if not new_name:
            raise BadRequest("device_name cannot be empty")
        updates.append("desired_name = ?")
        params.append(new_name)

    if "sleep_seconds" in payload:
        sleep_seconds = parse_sleep_seconds(payload)
        updates.append("desired_sleep_seconds = ?")
        params.append(sleep_seconds)

    if not updates:
        raise BadRequest("At least one of device_name or sleep_seconds is required")

    updates.append("updated_at = ?")
    params.append(utc_now())
    params.append(device_id)

    with get_db() as connection:
        cursor = connection.execute(
            f"UPDATE devices SET {', '.join(updates)} WHERE device_id = ?",
            tuple(params),
        )
        if cursor.rowcount == 0:
            raise BadRequest(f"Unknown device_id: {device_id}")

        row = connection.execute(
            """
            SELECT
                device_id,
                COALESCE(desired_name, device_name) AS display_name,
                COALESCE(desired_sleep_seconds, ?) AS desired_sleep_seconds
            FROM devices
            WHERE device_id = ?
            """,
            (DEFAULT_SLEEP_SECONDS, device_id),
        ).fetchone()

    return jsonify({"status": "ok", "device": dict(row)})


@app.route("/api/devices/<device_id>/measurements", methods=["GET"])
def get_device_measurements(device_id: str) -> Any:
    limit = request.args.get("limit", default=200, type=int)
    return jsonify({"device_id": device_id, "measurements": get_measurements(device_id, limit=limit)})


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
