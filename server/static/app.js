const deviceGrid = document.getElementById('deviceGrid');
const template = document.getElementById('deviceCardTemplate');
const refreshButton = document.getElementById('refreshButton');
const serverStatus = document.getElementById('serverStatus');
const lastRefresh = document.getElementById('lastRefresh');

function formatDate(value) {
  if (!value) {
    return '—';
  }

  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU');
}

function setMessage(element, text, isError = false) {
  element.textContent = text;
  element.className = `message ${isError ? 'error' : 'success'}`;
}

async function renameDevice(deviceId, deviceName, messageElement) {
  const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_name: deviceName }),
  });

  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.description || payload.message || 'Не удалось сохранить имя');
  }

  setMessage(messageElement, 'Имя сохранено. Устройство применит его при следующем пробуждении.');
}

function buildDeviceCard(device) {
  const fragment = template.content.cloneNode(true);
  const root = fragment.querySelector('.device-card');
  const nameElement = fragment.querySelector('.device-name');
  const idElement = fragment.querySelector('.device-id');
  const voltageBadge = fragment.querySelector('.voltage-badge');
  const lastSeen = fragment.querySelector('.last-seen');
  const ipAddress = fragment.querySelector('.ip-address');
  const wifiRssi = fragment.querySelector('.wifi-rssi');
  const firmware = fragment.querySelector('.firmware');
  const bootCount = fragment.querySelector('.boot-count');
  const form = fragment.querySelector('.rename-form');
  const input = form.querySelector('input[name="device_name"]');
  const message = fragment.querySelector('.message');

  nameElement.textContent = device.display_name;
  idElement.textContent = device.device_id;
  voltageBadge.textContent = `${Number(device.last_voltage ?? 0).toFixed(3)} V`;
  lastSeen.textContent = formatDate(device.last_seen);
  ipAddress.textContent = device.ip_address || '—';
  wifiRssi.textContent = device.wifi_rssi ?? '—';
  firmware.textContent = device.firmware_version || '—';
  bootCount.textContent = device.boot_count ?? '—';
  input.value = device.display_name;
  root.dataset.deviceId = device.device_id;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    message.textContent = '';

    try {
      await renameDevice(device.device_id, input.value.trim(), message);
      await loadDevices();
    } catch (error) {
      setMessage(message, error.message || 'Ошибка при сохранении', true);
    }
  });

  return fragment;
}

async function loadDevices() {
  const response = await fetch('/api/devices');
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.description || 'Ошибка при загрузке списка устройств');
  }

  deviceGrid.innerHTML = '';
  if (!payload.devices.length) {
    deviceGrid.innerHTML = '<div class="empty-state">Устройства пока не зарегистрированы. После первой отправки данных ESP32 появится здесь автоматически.</div>';
  } else {
    payload.devices.forEach((device) => deviceGrid.appendChild(buildDeviceCard(device)));
  }

  serverStatus.textContent = 'Онлайн';
  lastRefresh.textContent = formatDate(payload.server_time);
}

async function refresh() {
  try {
    await loadDevices();
  } catch (error) {
    serverStatus.textContent = 'Ошибка';
    lastRefresh.textContent = error.message;
  }
}

refreshButton.addEventListener('click', refresh);
refresh();
setInterval(refresh, 15000);
