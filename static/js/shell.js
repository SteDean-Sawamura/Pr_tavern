let currentTarget = null;
const frame = document.getElementById('content-frame');
const modeLabels = {
  tavern: '酒馆对话模式',
  rpg: 'RPG推演系统',
  scripts: '剧本管理',
  settings: '系统设置',
};

// 侧栏导航
document.querySelectorAll('.nav-btn[data-target]').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.target;
    const url = btn.dataset.url;
    if (!target || target === currentTarget) return;

    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    if (url) frame.src = url;
    currentTarget = target;
    document.getElementById('status-mode').textContent = modeLabels[target] || target;
  });
});

// 检查服务是否可用
async function checkServices() {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');

  try {
    const res = await fetch('/', { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      dot.className = 'status-dot';
      text.textContent = '就绪';
    }
  } catch {
    dot.className = 'status-dot loading';
    text.textContent = '连接中...';
    setTimeout(checkServices, 3000);
  }
}

// Ctrl+1/2 快捷键
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey) {
    if (e.key === '1') { e.preventDefault(); document.querySelector('[data-target="tavern"]')?.click(); }
    if (e.key === '2') { e.preventDefault(); document.querySelector('[data-target="rpg"]')?.click(); }
  }
});

// 初始化
currentTarget = 'tavern';
checkServices();
