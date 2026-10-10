const { app, BrowserWindow, Menu, shell, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');

const TAVERN_PORT = 8000;
const RPG_PORT = 8080;
const PROJECT_DIR = path.resolve(__dirname, '..');
const LOG_DIR = path.join(PROJECT_DIR, 'logs');
const LOG_FILE = path.join(LOG_DIR, `tavern_${new Date().toISOString().slice(0,10)}.log`);

let tavernProcess = null;
let rpgProcess = null;
let mainWindow = null;
let logStream = null;

function initLog() {
  if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
  logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  log('='.repeat(60));
  log(`酒馆桌面应用启动 — ${new Date().toLocaleString('zh-CN')}`);
  log(`项目目录: ${PROJECT_DIR}`);
}

function log(msg) {
  const line = `[${new Date().toTimeString().slice(0,8)}] ${msg}`;
  if (logStream) logStream.write(line + '\n');
  process.stdout.write(line + '\n');
}

function findPython() {
  const venvPython = path.join(PROJECT_DIR, 'bar', 'Scripts', 'python.exe');
  if (fs.existsSync(venvPython)) {
    log(`Python: ${venvPython}`);
    return venvPython;
  }
  log('Python: system python');
  return 'python';
}

function startProcess(script, label) {
  const python = findPython();
  log(`启动 ${label}: ${python} ${script}`);

  const proc = spawn(python, [script], {
    cwd: PROJECT_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  proc.stdout.on('data', d => {
    const text = d.toString().trimEnd();
    if (text) log(`[${label}] ${text}`);
  });

  proc.stderr.on('data', d => {
    const text = d.toString().trimEnd();
    if (text) log(`[${label}:err] ${text}`);
  });

  proc.on('error', err => log(`[${label}] 启动失败: ${err.message}`));
  proc.on('close', code => log(`[${label}] 进程退出 (code=${code})`));

  return proc;
}

function waitForServer(port, label, timeout = 30000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    function attempt() {
      const elapsed = ((Date.now() - start) / 1000).toFixed(1);
      if (Date.now() - start > timeout) {
        log(`[${label}] 端口 ${port} 等待超时 (${elapsed}s)`);
        return reject(new Error(`${label} port ${port} timeout`));
      }
      const sock = new net.Socket();
      sock.setTimeout(500);
      sock.once('connect', () => {
        sock.destroy();
        log(`[${label}] 端口 ${port} 就绪 (${elapsed}s)`);
        resolve();
      });
      sock.once('error', () => { sock.destroy(); setTimeout(attempt, 300); });
      sock.once('timeout', () => { sock.destroy(); setTimeout(attempt, 300); });
      sock.connect(port, '127.0.0.1');
    }
    attempt();
  });
}

function createWindow() {
  const menu = Menu.buildFromTemplate([
    {
      label: '应用',
      submenu: [
        { label: '打开日志文件', click: () => shell.openPath(LOG_FILE) },
        { label: '打开日志目录', click: () => shell.openPath(LOG_DIR) },
        { type: 'separator' },
        { label: '重新加载', accelerator: 'CmdOrCtrl+R', click: () => mainWindow?.reload() },
        { label: '开发者工具', accelerator: 'F12', click: () => mainWindow?.webContents.toggleDevTools() },
        { type: 'separator' },
        { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '视图',
      submenu: [
        { label: '放大', accelerator: 'CmdOrCtrl+=', click: () => mainWindow?.webContents.setZoomLevel(mainWindow.webContents.getZoomLevel() + 0.5) },
        { label: '缩小', accelerator: 'CmdOrCtrl+-', click: () => mainWindow?.webContents.setZoomLevel(mainWindow.webContents.getZoomLevel() - 0.5) },
        { label: '重置缩放', accelerator: 'CmdOrCtrl+0', click: () => mainWindow?.webContents.setZoomLevel(0) },
        { type: 'separator' },
        { label: '全屏', accelerator: 'F11', click: () => mainWindow?.setFullScreen(!mainWindow?.isFullScreen()) },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: '酒馆',
    frame: false,
    backgroundColor: '#1a1a1e',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'shell.html'));
  log('窗口已创建，等待服务就绪后切换到 web shell');

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });

  // 窗口控制 IPC
  ipcMain.on('window-minimize', () => mainWindow?.minimize());
  ipcMain.on('window-maximize', () => {
    if (mainWindow?.isMaximized()) {
      mainWindow.unmaximize();
    } else {
      mainWindow?.maximize();
    }
  });
  ipcMain.on('window-close', () => mainWindow?.close());
}

function sendStatus(status, message) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('server-status', { status, message });
  }
}

function killAll() {
  if (tavernProcess) {
    log('关闭酒馆服务...');
    tavernProcess.kill();
    tavernProcess = null;
  }
  if (rpgProcess) {
    log('关闭 RPG 服务...');
    rpgProcess.kill();
    rpgProcess = null;
  }
}

app.whenReady().then(async () => {
  initLog();

  createWindow();

  // 单进程启动：app.py 已挂载 RPG 引擎到 /rpg
  tavernProcess = startProcess('app.py', 'tavern');

  log('等待服务启动...');
  sendStatus('loading', '正在启动服务...');

  const results = await Promise.allSettled([
    waitForServer(TAVERN_PORT, 'tavern'),
  ]);

  const ok = results.filter(r => r.status === 'fulfilled').length;
  const fail = results.filter(r => r.status === 'rejected').length;
  log(`服务状态: ${ok} 就绪, ${fail} 失败`);

  if (fail > 0) {
    results.forEach((r, i) => {
      if (r.status === 'rejected') log(`  失败: ${r.reason.message}`);
    });
    sendStatus('error', `${fail} 个服务启动失败`);
  } else {
    // 服务就绪，切换到 web shell
    const shellUrl = `http://127.0.0.1:${TAVERN_PORT}/shell`;
    log(`加载 web shell: ${shellUrl}`);
    mainWindow.loadURL(shellUrl);
    sendStatus('ready');
  }

  log('应用就绪');
});

app.on('window-all-closed', () => {
  log('所有窗口关闭，退出应用');
  killAll();
  if (logStream) logStream.end();
  app.quit();
});

app.on('before-quit', () => {
  killAll();
  if (logStream) logStream.end();
});
