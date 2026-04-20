const LOW_VOLTAGE_DEFAULT = 11;

const deviceGrid = document.getElementById('deviceGrid');
const template = document.getElementById('deviceCardTemplate');
const refreshButton = document.getElementById('refreshButton');
const serverStatus = document.getElementById('serverStatus');
const lastRefresh = document.getElementById('lastRefresh');
const lowVoltageThreshold = document.getElementById('lowVoltageThreshold');

let currentLowVoltageThreshold = LOW_VOLTAGE_DEFAULT;

function buildDeviceRenderKey(device, index) {
  const lastSeen = device.last_seen || 'no-last-seen';
  return `${device.device_id}::${lastSeen}::${index}`;
}

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

async function saveDeviceSettings(deviceId, settingsPayload, messageElement) {
  const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settingsPayload),
  });

  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.description || payload.message || 'Не удалось сохранить настройки');
  }

  setMessage(messageElement, 'Настройки сохранены. ESP32 применит их при следующем пробуждении.');
}

function drawDischargeChart(canvas, measurements) {
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#0d1830';
  ctx.fillRect(0, 0, width, height);

  if (!measurements.length) {
    ctx.fillStyle = '#a7b9d7';
    ctx.font = '14px Inter, Arial, sans-serif';
    ctx.fillText('Пока нет данных для графика', 16, 28);
    return;
  }

  const values = measurements.map((m) => Number(m.voltage));
  const minV = Math.min(...values) - 0.05;
  const maxV = Math.max(...values) + 0.05;
  const range = Math.max(0.1, maxV - minV);
  const left = 40;
  const right = width - 16;
  const top = 16;
  const bottom = height - 30;

  ctx.strokeStyle = 'rgba(167, 185, 215, 0.35)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(left, top);
  ctx.lineTo(left, bottom);
  ctx.lineTo(right, bottom);
  ctx.stroke();

  ctx.strokeStyle = '#61b3ff';
  ctx.lineWidth = 2;
  ctx.beginPath();

  values.forEach((value, idx) => {
    const x = left + ((right - left) * idx) / Math.max(1, values.length - 1);
    const y = bottom - ((value - minV) / range) * (bottom - top);
    if (idx === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();

  ctx.fillStyle = '#a7b9d7';
  ctx.font = '12px Inter, Arial, sans-serif';
  ctx.fillText(`${maxV.toFixed(2)} V`, 4, top + 4);
  ctx.fillText(`${minV.toFixed(2)} V`, 4, bottom + 4);
  ctx.fillText(formatDate(measurements[0].seen_at), left, height - 8);
  const endLabel = formatDate(measurements[measurements.length - 1].seen_at);
  const textWidth = ctx.measureText(endLabel).width;
  ctx.fillText(endLabel, right - textWidth, height - 8);
}

async function loadChartForDevice(deviceId, canvas, messageElement) {
  const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/measurements?limit=250`);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.description || payload.message || 'Не удалось загрузить график');
  }

  drawDischargeChart(canvas, payload.measurements || []);
  messageElement.textContent = `Точек на графике: ${(payload.measurements || []).length}`;
  messageElement.className = 'message';
}

function buildDeviceCard(device, renderKey, expandedState, pendingEdit = null) {
  const fragment = template.content.cloneNode(true);
  const root = fragment.querySelector('.device-card');
  const devicePanel = fragment.querySelector('.device-accordion');
  const nameElement = fragment.querySelector('.device-name');
  const idElement = fragment.querySelector('.device-id');
  const voltageBadge = fragment.querySelector('.voltage-badge');
  const lastSeen = fragment.querySelector('.last-seen');
  const deviceStatus = fragment.querySelector('.device-status');
  const ipAddress = fragment.querySelector('.ip-address');
  const wifiRssi = fragment.querySelector('.wifi-rssi');
  const firmware = fragment.querySelector('.firmware');
  const bootCount = fragment.querySelector('.boot-count');
  const settingsForm = fragment.querySelector('.settings-form');
  const nameInput = settingsForm.querySelector('input[name="device_name"]');
  const sleepInput = settingsForm.querySelector('input[name="sleep_seconds"]');
  const settingsMessage = fragment.querySelector('.settings-message');
  const chartPanel = fragment.querySelector('.chart-accordion');
  const chartCanvas = fragment.querySelector('.discharge-chart');
  const chartMessage = fragment.querySelector('.chart-message');
  root.dataset.deviceId = device.device_id;
  root.dataset.renderKey = renderKey;

  devicePanel.open = expandedState.expandedDevices.has(renderKey);
  settingsForm.closest('.settings-accordion').open = expandedState.expandedSettings.has(renderKey);
  chartPanel.open = expandedState.expandedCharts.has(renderKey);
  chartPanel.dataset.loaded = expandedState.loadedCharts.has(renderKey) ? 'true' : 'false';

  nameElement.textContent = device.display_name;
  idElement.textContent = device.device_id;
  voltageBadge.textContent = `${Number(device.last_voltage ?? 0).toFixed(3)} V`;
  lastSeen.textContent = formatDate(device.last_seen);
  deviceStatus.textContent = device.status_text || 'Онлайн';
  ipAddress.textContent = device.ip_address || '—';
  wifiRssi.textContent = device.wifi_rssi ?? '—';
  firmware.textContent = device.firmware_version || '—';
  bootCount.textContent = device.boot_count ?? '—';
  const initialName = device.display_name;
  const initialSleep = String(Number(device.desired_sleep_seconds ?? 300));

  nameInput.value = pendingEdit?.device_name ?? initialName;
  sleepInput.value = pendingEdit?.sleep_seconds ?? initialSleep;
  nameInput.dataset.initialValue = initialName;
  sleepInput.dataset.initialValue = initialSleep;
  nameInput.dataset.dirty = 'false';
  sleepInput.dataset.dirty = 'false';

  const syncDirtyState = (input) => {
    input.dataset.dirty = String(input.value !== (input.dataset.initialValue ?? ''));
  };

  syncDirtyState(nameInput);
  syncDirtyState(sleepInput);
  nameInput.addEventListener('input', () => syncDirtyState(nameInput));
  sleepInput.addEventListener('input', () => syncDirtyState(sleepInput));

  const isLowVoltage = Boolean(device.is_low_voltage) || Number(device.last_voltage ?? 99) < currentLowVoltageThreshold;
  if (isLowVoltage) {
    root.classList.add('low-voltage');
    voltageBadge.classList.add('low');
  }

  if (device.is_offline) {
    root.classList.add('offline');
    deviceStatus.classList.add('offline');
  }

  settingsForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    settingsMessage.textContent = '';

    const payload = {
      device_name: nameInput.value.trim(),
      sleep_seconds: Number(sleepInput.value),
    };

    try {
      await saveDeviceSettings(device.device_id, payload, settingsMessage);
      await loadDevices();
    } catch (error) {
      setMessage(settingsMessage, error.message || 'Ошибка при сохранении', true);
    }
  });

  chartPanel.addEventListener('toggle', async () => {
    if (!chartPanel.open || chartPanel.dataset.loaded === 'true') {
      return;
    }
    try {
      await loadChartForDevice(device.device_id, chartCanvas, chartMessage);
      chartPanel.dataset.loaded = 'true';
    } catch (error) {
      setMessage(chartMessage, error.message || 'Ошибка загрузки графика', true);
    }
  });

  return fragment;
}

async function loadDevices() {
  const pendingEdits = new Map(
    Array.from(deviceGrid.querySelectorAll('.device-card'))
      .map((card) => {
        const renderKey = card.dataset.renderKey;
        const settingsForm = card.querySelector('.settings-form');
        const nameInput = settingsForm?.querySelector('input[name="device_name"]');
        const sleepInput = settingsForm?.querySelector('input[name="sleep_seconds"]');
        if (!renderKey || !nameInput || !sleepInput) {
          return null;
        }

        const isDirty = nameInput.dataset.dirty === 'true' || sleepInput.dataset.dirty === 'true';
        if (!isDirty) {
          return null;
        }

        return [renderKey, {
          device_name: nameInput.value,
          sleep_seconds: sleepInput.value,
        }];
      })
      .filter(Boolean),
  );

  const expandedDevices = new Set(
    Array.from(deviceGrid.querySelectorAll('.device-card .device-accordion[open]'))
      .map((panel) => panel.closest('.device-card')?.dataset.renderKey)
      .filter(Boolean),
  );
  const expandedSettings = new Set(
    Array.from(deviceGrid.querySelectorAll('.device-card .settings-accordion[open]'))
      .map((panel) => panel.closest('.device-card')?.dataset.renderKey)
      .filter(Boolean),
  );
  const expandedCharts = new Set(
    Array.from(deviceGrid.querySelectorAll('.device-card .chart-accordion[open]'))
      .map((panel) => panel.closest('.device-card')?.dataset.renderKey)
      .filter(Boolean),
  );
  const loadedCharts = new Set(
    Array.from(deviceGrid.querySelectorAll('.device-card .chart-accordion[data-loaded="true"]'))
      .map((panel) => panel.closest('.device-card')?.dataset.renderKey)
      .filter(Boolean),
  );

  const response = await fetch('/api/devices');
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.description || payload.message || 'Ошибка при загрузке списка устройств');
  }

  currentLowVoltageThreshold = Number(payload.low_voltage_threshold ?? LOW_VOLTAGE_DEFAULT);
  lowVoltageThreshold.textContent = `${currentLowVoltageThreshold.toFixed(1)} V`;

  deviceGrid.innerHTML = '';
  if (!payload.devices.length) {
    deviceGrid.innerHTML = '<div class="empty-state">Устройства пока не зарегистрированы. После первой отправки данных ESP32 появится здесь автоматически.</div>';
  } else {
    payload.devices.forEach((device, index) => {
      const renderKey = buildDeviceRenderKey(device, index);
      deviceGrid.appendChild(buildDeviceCard(
        device,
        renderKey,
        {
          expandedDevices,
          expandedSettings,
          expandedCharts,
          loadedCharts,
        },
        pendingEdits.get(renderKey),
      ));
    });

    if (expandedCharts.size) {
      const cards = Array.from(deviceGrid.querySelectorAll('.device-card'));
      await Promise.all(
        cards
          .filter((card) => expandedCharts.has(card.dataset.renderKey))
          .map(async (card) => {
            const chartPanel = card.querySelector('.chart-accordion');
            const chartCanvas = card.querySelector('.discharge-chart');
            const chartMessage = card.querySelector('.chart-message');
            if (!chartPanel || !chartCanvas || !chartMessage) {
              return;
            }

            chartPanel.open = true;
            try {
              await loadChartForDevice(card.dataset.deviceId, chartCanvas, chartMessage);
              chartPanel.dataset.loaded = 'true';
            } catch (error) {
              setMessage(chartMessage, error.message || 'Ошибка загрузки графика', true);
            }
          }),
      );
    }
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
