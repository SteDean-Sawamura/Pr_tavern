const TARGETS = {
  tavern:   'http://127.0.0.1:8000',
  rpg:      'http://127.0.0.1:8080',
  scripts:  'http://127.0.0.1:8000/#scripts',
  settings: 'http://127.0.0.1:8000/#settings',
};

let currentTarget = null;

// 导航
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.target;
    if (!target) return;
    navigateTo(target);
  });
});

function navigateTo(target) {
  const url = TARGETS[target];
  if (!url) return;

  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.querySelector(`[data-target="${target}"]`)?.classList.add('active');

  const frame = document.getElementById('content-frame');
  if (currentTarget !== target) {
    frame.src = url;
    currentTarget = target;
  }

  // 更新状态栏
  const modeNames = { tavern: '酒馆对话模式', rpg: 'RPG推演系统', scripts: '剧本管理', settings: '系统设置' };
  document.getElementById('status-mode').textContent = modeNames[target] || target;
}

// 窗口控制（通过 preload 暴露的安全 API）
document.getElementById('btn-minimize').onclick = () => window.electronAPI.minimize();
document.getElementById('btn-maximize').onclick = () => window.electronAPI.maximize();
document.getElementById('btn-close').onclick = () => window.electronAPI.close();

// 服务状态更新
window.electronAPI.onServerStatus(data => {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');

  if (data.status === 'ready') {
    dot.className = 'status-dot ready';
    text.textContent = '就绪';
    if (!currentTarget) navigateTo('tavern');
  } else if (data.status === 'loading') {
    dot.className = 'status-dot loading';
    text.textContent = data.message || '启动中...';
  } else if (data.status === 'error') {
    dot.className = 'status-dot error';
    text.textContent = data.message || '服务异常';
  }
});

// 模型信息更新
window.electronAPI.onModelInfo(data => {
  document.getElementById('status-model').textContent = `模型: ${data.model || '未配置'}`;
});

// 键盘快捷键
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey) {
    if (e.key === '1') { e.preventDefault(); navigateTo('tavern'); }
    if (e.key === '2') { e.preventDefault(); navigateTo('rpg'); }
  }
});
