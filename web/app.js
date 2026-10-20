'use strict';

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

// Значок из спрайта в index.html
const icon = (name) => `<svg class="ic" aria-hidden="true"><use href="#i-${name}"/></svg>`;

// ================================================================ утилиты

/** 0:42, 12:05, 1:03:20: часы показываем, только когда они есть */
function clockText(sec) {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor(s / 60) % 60;
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

function hhmmss(sec) {
  const s = Math.floor(sec);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

/** «00:01:23» → «01:23»: нулевые часы в стенограмме только мешают */
const shortTime = (t) => (t && t.startsWith('00:') ? t.slice(3) : t);

function plural(n, one, few, many) {
  if (n % 100 >= 11 && n % 100 <= 14) return many;
  const last = n % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

const count = (n, one, few, many) => `${n} ${plural(n, one, few, many)}`;

let toastTimer = null;

// Зовётся и из питона: фоновая проверка пакетов шлёт сюда своё сообщение
function toast(message, ms = 2600) {
  const t = $('toast');
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// Локальный файл не всегда даёт доступ к буферу обмена, поэтому со страховкой
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

async function copyAndSay(text, done) {
  toast(await copyText(text) ? done : 'Не вышло скопировать');
}

// Windows отдаёт имена вроде «Динамики (G733 Gaming Headset) [Loopback]».
// В списке, который и так подписан, это лишний шум
function prettyDevice(name) {
  return name
    .replace(/\s*\[Loopback\]\s*/i, '')
    .replace(/^(?:Динамики|Микрофон|Speakers|Microphone|Headphones|Наушники)\s*\((.+)\)$/i, '$1')
    .trim();
}

function fillSelect(select, items, defaultIndex) {
  select.innerHTML = '';
  for (const d of items) {
    const opt = new Option(prettyDevice(d.name), d.index);
    opt.title = d.name;  // полное имя по наведению, по нему же устройство ищется заново
    if (d.index === defaultIndex) opt.selected = true;
    select.add(opt);
  }
  select.disabled = !items.length;
  if (!items.length) select.add(new Option('устройств не найдено', ''));
}

const deviceName = (select) => select.selectedOptions[0]?.title || '';
const hasDevices = (select) => select.options.length && select.options[0].value !== '';

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* хранилище недоступно, запоминать не обязательно */ }
  return null;
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// =============================================================== движение

// «Уменьшить анимацию» в Windows: переходы и пробег цвета по дорожкам выключаются
const calm = window.matchMedia('(prefers-reduced-motion: reduce)');

/**
 * Смена состояния с переходом браузера: старое и новое состояние
 * плавно сменяют друг друга. kind выбирает рисунок перехода в style.css.
 */
function vt(update, kind = '') {
  // В свёрнутом окне браузер переход всё равно пропустит
  if (calm.matches || !document.startViewTransition || document.visibilityState !== 'visible') {
    update();
    return;
  }
  const root = document.documentElement;
  root.dataset.vt = kind;
  const t = document.startViewTransition(update);
  // Пропущенный переход отклоняет ready. Состояние при этом уже новое, ошибки нет
  t.ready.catch(() => {});
  t.finished.finally(() => {
    if (root.dataset.vt === kind) delete root.dataset.vt;
  });
}

/** Проигрывает CSS-анимацию появления ещё раз: класс снимается и ставится заново. */
function replay(el, cls = 'enter') {
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}

/** Что-то печатают: горячие клавиши тогда молчат. */
const typing = () => ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);

// =============================================================== дорожки

// Питон присылает громкость каждого источника, окно раз в 100 мс кладёт её
// в дорожку. Высота столбика равна громкости, на 0.12 столбик во всю высоту:
// так звучит обычная речь в гарнитуре
const SAMPLE_MS = 100;
const LEVEL_FULL = 0.12;
const SILENCE = 0.04;
const norm = (v) => Math.min(1, v / LEVEL_FULL);

let lastLevels = [0, 0];

function fitCanvas(c) {
  const r = c.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * dpr));
  const h = Math.max(1, Math.round(r.height * dpr));
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  return { ctx: c.getContext('2d'), w, h, dpr };
}

function bar(ctx, x, mid, width, height) {
  ctx.beginPath();
  ctx.roundRect(x, mid - height / 2, width, height, width / 2);
  ctx.fill();
}

/**
 * Живая дорожка: свежие столбики справа, старые уходят влево.
 *
 * tone решает цвет. До записи дорожки серые: это саундчек, ничего не пишется.
 * upTo это доля ширины слева, уже залитая цветом дорожки: на старте записи
 * она бежит от 0 до 1. alpha гасит цвет обратно в серый после остановки.
 */
function drawLive(canvas, samples, tone = { upTo: 1, alpha: 1 }) {
  const { ctx, w, h, dpr } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const mid = h / 2;
  ctx.fillStyle = cssVar('--line-strong');
  ctx.fillRect(0, Math.round(mid - dpr / 2), w, dpr);

  const color = getComputedStyle(canvas).color;
  const muted = cssVar('--ink-2');
  const edge = 80 * dpr;  // мягкий край у бегущей заливки
  const step = 5 * dpr;
  const width = 3 * dpr;
  const fit = Math.floor(w / step);
  for (let i = Math.max(0, samples.length - fit); i < samples.length; i++) {
    const v = norm(samples[i]);
    if (v < SILENCE) continue;
    const x = w - (samples.length - i) * step;
    const height = Math.max(2 * dpr, v * (h - 4 * dpr));
    const lit = Math.min(1, Math.max(0, (tone.upTo * (w + edge) - x) / edge)) * tone.alpha;
    if (lit < 1) {
      ctx.globalAlpha = 0.42 * (1 - lit);
      ctx.fillStyle = muted;
      bar(ctx, x, mid, width, height);
    }
    if (lit > 0) {
      ctx.globalAlpha = lit;
      ctx.fillStyle = color;
      bar(ctx, x, mid, width, height);
    }
  }
  ctx.globalAlpha = 1;
}

/**
 * Дорожка дубля на шкале времени. Серое окно это цель по времени,
 * всё, что сказано после её конца, красное.
 */
function drawTake(canvas, samples, maxSec, target) {
  const { ctx, w, h, dpr } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const px = w / maxSec;
  const mid = h / 2;

  if (target) {
    ctx.fillStyle = cssVar('--fill');
    ctx.fillRect(target[0] * px, 0, (target[1] - target[0]) * px, h);
    ctx.fillStyle = cssVar('--line-strong');
    ctx.fillRect(Math.round(target[0] * px), 0, dpr, h);
    ctx.fillRect(Math.round(target[1] * px), 0, dpr, h);
  }
  ctx.fillStyle = cssVar('--line-strong');
  ctx.fillRect(0, Math.round(mid - dpr / 2), w, dpr);

  const me = cssVar('--me');
  const over = cssVar('--rec');
  const slot = px * (SAMPLE_MS / 1000);
  const width = Math.max(dpr, Math.min(3 * dpr, slot * 0.6));
  samples.forEach((s, i) => {
    const v = norm(s);
    if (v < SILENCE) return;
    const t = (i * SAMPLE_MS) / 1000;
    ctx.fillStyle = target && t > target[1] ? over : me;
    bar(ctx, t * px, mid, width, Math.max(2 * dpr, v * (h - 6 * dpr)));
  });
}

/** Подписи шкалы под дорожкой дубля: ноль, границы цели и конец шкалы. */
function paintScale(box, maxSec, target) {
  const marks = target ? [0, target[0], target[1], Math.round(maxSec)] : [0, Math.round(maxSec / 2), Math.round(maxSec)];
  box.innerHTML = '';
  for (const m of [...new Set(marks)]) {
    const s = document.createElement('span');
    s.textContent = `${m} с`;
    s.style.left = `${Math.min(100, (m / maxSec) * 100)}%`;
    if (m >= maxSec - 1) { s.style.left = 'auto'; s.style.right = '0'; s.style.transform = 'none'; }
    box.appendChild(s);
  }
}

// =============================================== диалог вместо prompt()

/**
 * Свой диалог: вопрос, поле ввода по желанию, кнопки.
 * Возвращает введённый текст, true для подтверждения или null для отмены.
 */
function ask({ title, text = '', input = null, ok = 'Готово', danger = false }) {
  const dlg = $('dlg');
  $('dlgTitle').textContent = title;
  $('dlgText').textContent = text;
  $('dlgText').hidden = !text;
  const field = $('dlgInput');
  field.hidden = input === null;
  field.value = input ?? '';
  const okBtn = $('dlgOk');
  okBtn.textContent = ok;
  okBtn.className = 'btn ' + (danger ? 'danger' : 'primary');
  // Enter в поле подтверждает, а не жмёт первую кнопку формы («Отмена»)
  field.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); dlg.close('ok'); }
  };

  return new Promise((resolve) => {
    dlg.addEventListener('close', function onClose() {
      dlg.removeEventListener('close', onClose);
      if (dlg.returnValue !== 'ok') return resolve(null);
      resolve(input === null ? true : field.value.trim());
    });
    dlg.returnValue = '';
    dlg.showModal();
    if (input !== null) { field.focus(); field.select(); } else okBtn.focus();
  });
}

// =============================================================== разделы

let screen = 'meeting';

// Порядок разделов: переход едет вправо, если идёшь дальше по списку, и влево, если назад
const SCREENS = ['meeting', 'interview', 'archive', 'settings'];

function show(name) {
  if (name === screen) return;
  if (ivAnswering) { toast('Сначала закончи ответ'); return; }
  // Тренажёру нужен тот же микрофон, что пишет встречу
  if (name === 'interview' && recording) { toast('Сначала останови запись встречи'); return; }
  const forward = SCREENS.indexOf(name) > SCREENS.indexOf(screen);
  screen = name;
  closeMenu();
  vt(() => {
    for (const b of document.querySelectorAll('[data-screen]')) {
      if (b.dataset.screen === name) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    }
    for (const s of SCREENS) $('screen-' + s).hidden = s !== name;
    paintNavLive();
    if (name === 'meeting') redrawIdleTracks();
  }, forward ? 'next' : 'back');
  paintModeMark();
  syncMonitor();
  if (name === 'interview' && !iv.setup.hidden) ivLoadSetup();
  if (name === 'archive') arLoad(ar.search.value);
  if (name === 'settings') stLoad();
}

/** Черта под вкладкой переезжает к выбранной, а не появляется рывком. */
function paintModeMark() {
  const mark = $('modeMark');
  const tab = document.querySelector(`.mode[data-screen="${screen}"]`);
  mark.classList.toggle('off', !tab);
  if (!tab) return;
  mark.style.width = `${tab.offsetWidth - 24}px`;
  mark.style.transform = `translateX(${tab.offsetLeft + 12}px)`;
}

document.querySelectorAll('[data-screen]').forEach((b) => {
  b.addEventListener('click', () => show(b.dataset.screen));
});

$('asrOpen').addEventListener('click', () => show('settings'));
$('ckAsrEdit').addEventListener('click', () => show('settings'));
$('navLive').addEventListener('click', () => show('meeting'));

// Ctrl+1, 2, 3 и Ctrl+запятая: разделы. Ctrl+F в архиве: поиск
document.addEventListener('keydown', (e) => {
  if (!e.ctrlKey || e.shiftKey || e.altKey || $('dlg').open) return;
  const to = { Digit1: 'meeting', Digit2: 'interview', Digit3: 'archive', Comma: 'settings' }[e.code];
  if (to) { e.preventDefault(); show(to); return; }
  if (e.code === 'KeyF' && screen === 'archive') {
    e.preventDefault();
    ar.search.focus();
    ar.search.select();
  }
});

// Огонёк «идёт запись» нужен, только когда смотришь другой раздел
function paintNavLive() {
  $('navLive').hidden = !(recording && screen !== 'meeting');
}

function paintAsrLabel(label) {
  $('asrOpen').textContent = label || '…';
  $('ckAsrText').textContent = label || '…';
}

// ========================================================= запись встречи

const mt = {
  screen: $('screen-meeting'),
  mic: $('mic'), spk: $('spk'),
  micBtn: $('micBtn'), spkBtn: $('spkBtn'), micMenu: $('micMenu'), spkMenu: $('spkMenu'),
  tracks: $('tracks'),
  record: $('record'), recDot: $('recDot'), recLabel: $('recLabel'), clock: $('clock'),
  state: $('state'), stateText: $('stateText'), stateSub: $('stateSub'),
  fileActions: $('fileActions'),
  waveMe: $('waveMe'), waveThem: $('waveThem'),
  scroll: $('scroll'), intro: $('intro'), lines: $('lines'), toBottom: $('toBottom'),
};

let recording = false;
let phase = 'idle';       // idle | starting | live | stopping | done
let currentPath = '';
let lineCount = 0;
let gapCount = 0;
let lastSpeaker = null;
let stickToBottom = true;
let liveStart = 0;
let liveMe = [];
let liveThem = [];

// Тон дорожек записи, смысл полей описан у drawLive
const tone = { upTo: 0, alpha: 1 };
let toneFrame = 0;

/** Плавно меняет одно поле тона и перерисовывает дорожки каждый кадр. */
function animateTone(key, from, to, ms) {
  cancelAnimationFrame(toneFrame);
  tone[key] = from;
  if (calm.matches) { tone[key] = to; redrawIdleTracks(); return; }
  const t0 = performance.now();
  const frame = (now) => {
    const k = Math.min(1, (now - t0) / ms);
    tone[key] = from + (to - from) * (1 - (1 - k) ** 3);
    redrawIdleTracks();
    if (k < 1) toneFrame = requestAnimationFrame(frame);
  };
  toneFrame = requestAnimationFrame(frame);
}

function setPhase(p) {
  phase = p;
  mt.screen.dataset.state = p;
  const live = p === 'live';
  mt.record.className = 'btn rec big';
  mt.record.disabled = p === 'starting' || p === 'stopping';
  mt.recDot.classList.toggle('stop', live);
  mt.recLabel.textContent = {
    idle: 'Начать запись', starting: 'Запускаю…', live: 'Остановить',
    stopping: 'Останавливаю…', done: 'Новая запись',
  }[p];
  mt.clock.hidden = !(live || p === 'stopping' || p === 'done');
  mt.fileActions.hidden = !currentPath || p === 'idle' || p === 'starting';
  const lock = live || p === 'starting' || p === 'stopping';
  mt.mic.disabled = lock || !hasDevices(mt.mic);
  mt.spk.disabled = lock || !hasDevices(mt.spk);
  paintDeviceButtons();
  $('asrOpen').disabled = lock;
  mt.tracks.dataset.tone = p === 'idle' || p === 'starting' ? 'check' : 'live';
  syncMonitor();
}

// --- саундчек: до записи дорожки живые, но серые, и ничего не пишется ---

let monitorOn = false;
let monitorQueue = Promise.resolve();
const heard = { me: false, them: false };

// Порог «слышно»: речь в гарнитуре даёт около 0.1, любой звук в наушниках
// заметно выше нуля. Ниже этого VAD речью ничего не признает
const HEARD_ME = 0.04;
const HEARD_THEM = 0.01;

function monitorWanted() {
  return screen === 'meeting' && phase === 'idle' && document.visibilityState === 'visible'
    && (hasDevices(mt.mic) || hasDevices(mt.spk));
}

/**
 * Открывает или закрывает саундчек под текущее состояние окна.
 * Вызовы выстраиваются в очередь: питон должен получать их по порядку,
 * иначе саундчек может открыться поверх только что начатой записи.
 */
function syncMonitor(restart = false) {
  monitorQueue = monitorQueue.then(async () => {
    if (!window.pywebview) return;
    const want = monitorWanted();
    if (want && (!monitorOn || restart)) {
      const r = await api().monitor_start(
        hasDevices(mt.mic) ? Number(mt.mic.value) : -1, hasDevices(mt.spk) ? Number(mt.spk.value) : -1,
        deviceName(mt.mic), deviceName(mt.spk),
      );
      monitorOn = !(r && r.error);
      if (r && r.error) paintHeard(r.error);
    } else if (!want && monitorOn) {
      monitorOn = false;
      await api().monitor_stop();
      lastLevels = [0, 0];
    }
  }).catch(() => { monitorOn = false; });
  return monitorQueue;
}

document.addEventListener('visibilitychange', () => syncMonitor());

/** Строки готовности под дорожками: слышно ли меня и собеседника. */
function paintHeard(error) {
  const rows = [
    ['ckMe', heard.me, hasDevices(mt.mic), 'скажи что-нибудь', 'микрофон не найден'],
    ['ckThem', heard.them, hasDevices(mt.spk), 'включи видео или музыку', 'наушники не найдены, подключи их'],
  ];
  for (const [id, ok, has, wait, none] of rows) {
    const li = $(id);
    const state = error ? 'bad' : !has ? 'bad' : ok ? 'ok' : 'wait';
    if (li.dataset.state !== state) li.dataset.state = state;
    li.querySelector('.ck-text').textContent = error || (!has ? none : ok ? 'слышно' : wait);
  }
}

function resetHeard(which) {
  if (which !== 'them') heard.me = false;
  if (which !== 'me') heard.them = false;
  paintHeard();
}

// --- кнопки устройств: своё меню вместо системного списка ---

function paintDeviceButtons() {
  for (const [select, btn, role] of [[mt.mic, mt.micBtn, 'Мой микрофон'], [mt.spk, mt.spkBtn, 'Звук собеседника']]) {
    const opt = select.selectedOptions[0];
    const name = hasDevices(select) && opt ? opt.textContent : 'не найдено';
    btn.querySelector('.dev-name').textContent = name;
    // Подпись для экранного диктора: и чья дорожка, и что выбрано
    btn.setAttribute('aria-label', `${role}: ${name}`);
    btn.title = opt ? opt.title : '';
    btn.disabled = select.disabled;
  }
}

function openDeviceMenu(select, btn, menu, which) {
  const open = menu.hidden;
  closeMenu();
  if (!open) return;
  menu.innerHTML = '';
  for (const opt of select.options) {
    const item = document.createElement('button');
    item.type = 'button';
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', String(opt.selected));
    item.innerHTML = `<span class="nm"></span>${icon('check')}`;
    item.querySelector('.nm').textContent = opt.textContent;
    item.title = opt.title;
    item.addEventListener('click', () => {
      closeMenu();
      btn.focus();
      if (select.value === opt.value) return;
      select.value = opt.value;
      store(which === 'me' ? 'micName' : 'spkName', opt.title);
      paintDeviceButtons();
      resetHeard(which);
      syncMonitor(true);
    });
    menu.appendChild(item);
  }
  menu.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  (menu.querySelector('[aria-selected="true"]') || menu.querySelector('button'))?.focus();
}

mt.micBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  openDeviceMenu(mt.mic, mt.micBtn, mt.micMenu, 'me');
});
mt.spkBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  openDeviceMenu(mt.spk, mt.spkBtn, mt.spkMenu, 'them');
});

/** Выбирает устройство, которое человек брал в прошлый раз, если оно на месте. */
function pickRemembered(select, key) {
  const name = store(key);
  const opt = name && [...select.options].find((o) => o.title === name);
  if (opt) select.value = opt.value;
}

function paintState(text, kind, sub) {
  mt.state.dataset.kind = kind || 'idle';
  mt.stateText.textContent = text;
  mt.stateSub.textContent = sub || '';
}

function liveSub() {
  return lineCount
    ? `${count(lineCount, 'реплика', 'реплики', 'реплик')}, файл дописывается`
    : 'Жду первую реплику';
}

function redrawIdleTracks() {
  drawLive(mt.waveMe, liveMe, tone);
  drawLive(mt.waveThem, liveThem, tone);
}

// --- прокрутка ленты ---

function paintToBottom() {
  const gap = mt.scroll.scrollHeight - mt.scroll.scrollTop - mt.scroll.clientHeight;
  mt.toBottom.hidden = stickToBottom || gap < 40 || !lineCount;
}

mt.scroll.addEventListener('scroll', () => {
  const gap = mt.scroll.scrollHeight - mt.scroll.scrollTop - mt.scroll.clientHeight;
  stickToBottom = gap < 40;
  paintToBottom();
});

mt.toBottom.addEventListener('click', () => {
  stickToBottom = true;
  mt.toBottom.hidden = true;
  mt.scroll.scrollTo({ top: mt.scroll.scrollHeight });
});

function scrollIfNeeded() {
  if (stickToBottom) mt.scroll.scrollTo({ top: mt.scroll.scrollHeight });
  paintToBottom();
}

/** Строка стенограммы: время, кто, текст. Подряд от одного человека имя не повторяем. */
function makeUtt({ who, speaker, time, text, gap, prev }) {
  const row = document.createElement('div');
  row.className = `utt ${who}` + (who === prev ? ' cont' : '') + (gap ? ' gap' : '');
  row.innerHTML = '<span class="utt-time"></span><span class="utt-who"></span><p class="utt-text"></p>';
  row.children[0].textContent = time;
  row.children[1].textContent = speaker;
  row.children[2].textContent = text;
  return row;
}

// --- кнопки ---

mt.record.addEventListener('click', async () => {
  if (phase === 'live') {
    setPhase('stopping');
    paintState('Останавливаю', 'stopping', 'дописываю последние реплики в файл');
    await api().stop();
    return;
  }
  if (phase !== 'idle' && phase !== 'done') return;
  setPhase('starting');
  paintState('Запускаю', 'loading', $('asrOpen').textContent);
  // Саундчек должен закрыться раньше, чем откроется запись
  await monitorQueue;
  // Имена шлём вместе с номерами: номера устройств в Windows плавают,
  // и к моменту записи выбранный номер может указывать на другой вход
  const res = await api().start(
    Number(mt.mic.value), Number(mt.spk.value), deviceName(mt.mic), deviceName(mt.spk),
  );
  if (res && res.error) ui.error(res.error);
});

// Пробел начинает и останавливает запись, пока ничего не печатаешь
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || screen !== 'meeting' || typing() || $('dlg').open) return;
  const el = document.activeElement;
  if (el && el !== mt.record && ['BUTTON', 'SUMMARY'].includes(el.tagName)) return;
  e.preventDefault();
  if (!e.repeat) mt.record.click();
});

$('copyPath').addEventListener('click', () => {
  if (currentPath) copyAndSay(currentPath, 'Путь скопирован, его можно отдать агенту');
});

$('openDir').addEventListener('click', () => api().open_dir());

// Ctrl+A выделяет стенограмму, а не всё окно
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.code === 'KeyA' && screen === 'meeting' && lineCount) {
    e.preventDefault();
    const range = document.createRange();
    range.selectNodeContents(mt.lines);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
});

function showReport(r) {
  const box = document.createElement('section');
  box.className = 'report';
  const rows = [
    ['Твоя речь', `${count(r.replicas, 'реплика', 'реплики', 'реплик')}, ${Math.round(r.duration)} с, ${count(r.words, 'слово', 'слова', 'слов')}`],
    ['Темп', `${Math.round(r.wpm)} слов в минуту${r.tempo ? ', ' + r.tempo : ''}`],
    ['Слова-паразиты', r.fillers],
    ['Неуверенные слова', r.hedges],
    ['Связки', r.connectors],
  ];
  if (r.slips) rows.push(['Оговорки', r.slips]);
  box.innerHTML = '<h2>Как ты говорил</h2><dl class="facts"></dl>';
  const dl = box.querySelector('dl');
  for (const [name, value] of rows) {
    const d = document.createElement('div');
    d.innerHTML = '<dt></dt><dd></dd>';
    d.children[0].textContent = name;
    d.children[1].textContent = value;
    dl.appendChild(d);
  }
  mt.lines.appendChild(box);
  scrollIfNeeded();
}

// ======================================================= вызовы из питона

const ui = {
  /** kind: idle | loading | ready | live | stopping | done | error */
  setStatus(text, kind, sub) {
    if (kind === 'live') sub = liveSub();
    if (kind === 'stopping' && phase === 'live') setPhase('stopping');
    paintState(text, kind, sub);
  },

  started(path) {
    recording = true;
    monitorOn = false;  // питон закрыл саундчек сам, перед тем как открыть запись
    currentPath = path;
    lineCount = 0;
    gapCount = 0;
    lastSpeaker = null;
    stickToBottom = true;
    // Дорожки не обнуляем: столбики саундчека остаются и загораются цветом
    // слева направо, как пульт, который встал на запись
    liveStart = Date.now();
    mt.clock.textContent = '0:00';
    vt(() => {
      mt.lines.innerHTML = '';
      mt.intro.hidden = true;
      $('waitFirst').hidden = false;
      setPhase('live');
      paintState('Идёт запись', 'live', liveSub());
    }, 'rec');
    tone.alpha = 1;
    animateTone('upTo', 0, 1, 900);
    document.body.classList.add('recording');
    paintNavLive();
    paintToBottom();
  },

  stopped(report) {
    recording = false;
    $('waitFirst').hidden = true;
    document.body.classList.remove('recording');
    lastLevels = [0, 0];
    setPhase('done');
    paintNavLive();
    // Цвет гаснет: дорожки снова серые, запись на них больше не идёт
    tone.upTo = 1;
    animateTone('alpha', 1, 0, 700);
    if (report) showReport(report);
  },

  addLine({ t, speaker, text, who, gap }) {
    lineCount += 1;
    $('waitFirst').hidden = true;
    if (gap) gapCount += 1;
    mt.lines.appendChild(makeUtt({ who, speaker, time: shortTime(hhmmss(t)), text, gap, prev: lastSpeaker }));
    lastSpeaker = who;
    if (mt.state.dataset.kind === 'live') mt.stateSub.textContent = liveSub();
    scrollIfNeeded();
  },

  setLevels(me, them) {
    lastLevels = [me, them];
  },

  error(message) {
    if (phase === 'starting' || phase === 'idle' || phase === 'done') {
      if (phase === 'starting') setPhase(lineCount ? 'done' : 'idle');
      paintState('Запись не началась', 'error', message);
      return;
    }
    if (phase === 'stopping') setPhase('done');
    toast(message, 6000);
  },

  /** Беда с отдельной репликой: запись идёт дальше, состояние не трогаем. */
  warn(message) {
    toast(message, 6000);
  },

  /** В папке с банками появился новый файл или переписали старый. */
  banksChanged(banks, savedThemes, appeared) {
    ivBanks = banks || [];
    ivSavedThemes = savedThemes || {};
    const fresh = appeared && appeared.length ? appeared[appeared.length - 1] : null;
    // Свежий банк логично сразу и выбрать: за ним и шли
    if (fresh && !iv.setup.hidden) ivSelectBank(fresh);
    else ivRenderBanks();
    if (fresh) {
      const b = ivBanks.find((x) => x.file === fresh);
      toast(b ? `Новый банк: ${b.name}` : 'Список банков обновлён');
    }
  },
};

window.ui = ui;

// Один такт на всё окно: часы, дорожки записи, проверка микрофона, дубль
setInterval(() => {
  if (recording || monitorOn) {
    liveMe.push(lastLevels[0]);
    liveThem.push(lastLevels[1]);
    if (liveMe.length > 3000) { liveMe = liveMe.slice(-2000); liveThem = liveThem.slice(-2000); }
    if (screen === 'meeting') redrawIdleTracks();
  }
  if (monitorOn && !recording) {
    const me = lastLevels[0] > HEARD_ME && !heard.me;
    const them = lastLevels[1] > HEARD_THEM && !heard.them;
    if (me) heard.me = true;
    if (them) heard.them = true;
    if (me || them) paintHeard();
  }
  if (recording) {
    const passed = (Date.now() - liveStart) / 1000;
    mt.clock.textContent = clockText(passed);
    $('navLiveText').textContent = clockText(passed);
  }
  if (micChecking) {
    checkSamples.push(lastLevels[0]);
    drawLive($('waveCheck'), checkSamples);
  }
  if (ivAnswering) ivTakeTick();
}, SAMPLE_MS);

new ResizeObserver(() => {
  if (screen === 'meeting') redrawIdleTracks();
  if (screen === 'interview' && !iv.ask.hidden) ivDrawTake();
  if (screen === 'interview' && !iv.result.hidden && ivLastTake) {
    drawTake($('waveResult'), ivLastTake.samples, ivLastTake.maxSec, ivLastTake.target);
  }
}).observe(document.querySelector('.main'));

// ============================================================== тренажёр

const iv = {
  setup: $('ivSetup'), ask: $('ivAsk'), result: $('ivResult'), done: $('ivDone'),
  banks: $('ivBanks'), own: $('ivOwn'), bankNote: $('ivBankNote'),
  presets: $('ivPresets'), themes: $('ivThemes'), levels: $('ivLevels'), levelDesc: $('ivLevelDesc'),
  mic: $('ivMic'), start: $('ivStart'),
  history: $('ivHistory'), historyCard: $('ivHistoryCard'),
  progress: $('ivProgress'), pips: $('ivPips'), category: $('ivCategory'),
  question: $('ivQuestion'), target: $('ivTarget'), timer: $('ivTimer'), hint: $('ivHint'),
  answer: $('ivAnswer'), answerLabel: $('ivAnswerLabel'), skip: $('ivSkip'), finish: $('ivFinish'),
  waveTake: $('waveTake'), takeScale: $('takeScale'),
  verdict: $('ivVerdict'), verdictIcon: $('ivVerdictIcon'), clean: $('ivClean'), attempt: $('ivAttempt'),
  delta: $('ivDelta'), closed: $('ivClosed'), rows: $('ivRows'), theses: $('ivTheses'),
  text: $('ivText'), followup: $('ivFollowup'),
  retry: $('ivRetry'), next: $('ivNext'), finish2: $('ivFinish2'),
};

// Что считать «поведенческим». Остальные темы идут как технические:
// делить приходится здесь, потому что в банке у вопроса только имя темы
const SOFT_THEMES = ['поведенческий', 'мотивация', 'о себе', 'софт'];

const IV_IDLE_HINT = 'Нажми красную кнопку или пробел и говори. Свой текст ты не увидишь, как на настоящем собесе.';

// Запрос, по которому любая нейросеть соберёт банк в нужном формате
const BANK_PROMPT = `Сделай банк вопросов для тренировки собеседования в формате YAML.
Ключи строго такие: название, вопросы. У каждого вопроса: id, вопрос,
категория, уровень (1, 2 или 3), цель_сек ([от, до] в секундах),
паразитов_макс, тезисы. У каждого тезиса: текст, слова, добить.
слова это 3-5 корней слов, по которым видно, что тезис прозвучал,
с обрезанными окончаниями. добить это уточняющий вопрос, если тезис
не прозвучал. Поведенческие вопросы помечай категорией «поведенческий».
Ответь только YAML, без пояснений.
Сделай 15 вопросов под эту вакансию:
`;

let ivBanks = [];        // банки со списком вопросов, чтобы считать без запросов
let ivSavedThemes = {};  // выбор тем, запомненный по каждому банку
let ivBankFile = store('ivBank') || '';
let ivChosen = new Set();
let ivCustom = false;    // темы выбираются вручную, чипы открыты
let ivLevels = [];
let ivLevel = 'рабочий';
let ivTheses = [];       // тезисы текущего вопроса, чтобы подписать разбор
let ivAnswering = false;
let ivBusy = false;
let ivStarted = 0;
let ivTarget = null;
let ivPendingFollowup = '';  // текст уточняющего вопроса, на который сейчас отвечаем
let ivPrompt = '';
let ivPath = '';
let ivQuestionNo = 1;
let ivTake = { samples: [], maxSec: 90, target: null };
let ivLastTake = null;
let micChecking = false;
let checkSamples = [];

function ivShow(section) {
  for (const s of [iv.setup, iv.ask, iv.result, iv.done]) s.hidden = s !== section;
  $('screen-interview').scrollTop = 0;
}

const ivCurrentBank = () => ivBanks.find((b) => b.file === ivBankFile);
const ivCurrentLevel = () => ivLevels.find((x) => x.id === ivLevel) || null;

/** Вопросы, которые останутся при выбранных темах и строгости. */
function ivPicked() {
  const bank = ivCurrentBank();
  if (!bank) return [];
  const max = ivCurrentLevel() ? ivCurrentLevel().max_level : 99;
  return bank.questions.filter((q) => q.level <= max && (!ivChosen.size || ivChosen.has(q.category)));
}

/** Грубая оценка длительности: цель ответа плюс время на чтение и разбор. */
function ivEstimate(questions) {
  const level = ivCurrentLevel();
  const factor = level && level.time_factor ? level.time_factor : 1;
  let sec = 0;
  for (const q of questions) sec += (q.target ? (q.target[0] + q.target[1]) / 2 : 60) * factor + 25;
  const min = Math.max(1, Math.round(sec / 60));
  return min < 60
    ? `около ${count(min, 'минуты', 'минут', 'минут')}`
    : `около ${Math.floor(min / 60)} ч ${min % 60} мин`;
}

// --- банки строками ---

function ivRenderBanks() {
  iv.banks.innerHTML = '';
  if (!ivCurrentBank() && ivBanks.length) ivBankFile = ivBanks[0].file;
  for (const b of ivBanks) {
    const on = b.file === ivBankFile;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'bank' + (on ? ' on' : '') + (b.count ? '' : ' broken');
    row.setAttribute('role', 'radio');
    row.setAttribute('aria-checked', String(on));
    row.innerHTML = '<span class="radio"></span><span class="nm"></span><span class="meta"></span>';
    row.children[1].textContent = b.name;
    row.children[1].title = b.name;
    row.children[2].textContent = b.count ? count(b.count, 'вопрос', 'вопроса', 'вопросов') : 'файл не читается';
    row.addEventListener('click', () => ivSelectBank(b.file));
    iv.banks.appendChild(row);
  }
  if (!ivBanks.length) {
    const p = document.createElement('p');
    p.className = 'help';
    p.style.padding = '12px 14px';
    p.textContent = 'Банков пока нет. Сделай свой, это пара минут.';
    iv.banks.appendChild(p);
    iv.own.hidden = false;
  }
  ivRenderThemes();
}

function ivSelectBank(file) {
  ivBankFile = file;
  store('ivBank', file);
  ivRestoreThemes();
}

/** Возвращает выбор тем, сделанный для этого банка в прошлый раз. */
function ivRestoreThemes() {
  const saved = ivSavedThemes[ivBankFile];
  const bank = ivCurrentBank();
  ivChosen = new Set(Array.isArray(saved) && bank ? saved.filter((c) => bank.categories.includes(c)) : []);
  ivCustom = ivPresetOf(ivChosen) === 'custom';
  ivRenderBanks();
}

$('ivOwnToggle').addEventListener('click', () => { iv.own.hidden = !iv.own.hidden; });

// --- темы ---

function ivSet(kind) {
  const bank = ivCurrentBank();
  if (!bank) return new Set();
  const soft = (c) => SOFT_THEMES.includes(c.toLowerCase());
  if (kind === 'soft') return new Set(bank.categories.filter(soft));
  if (kind === 'tech') return new Set(bank.categories.filter((c) => !soft(c)));
  return new Set();
}

function ivPresetOf(chosen) {
  if (!chosen.size) return 'all';
  const same = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));
  if (same(chosen, ivSet('soft'))) return 'soft';
  if (same(chosen, ivSet('tech'))) return 'tech';
  return 'custom';
}

function ivRenderThemes() {
  const bank = ivCurrentBank();
  const preset = ivCustom ? 'custom' : ivPresetOf(ivChosen);
  for (const b of iv.presets.querySelectorAll('button')) {
    const on = b.dataset.preset === preset;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  }
  iv.bankNote.textContent = bank ? `в банке ${count(bank.categories.length, 'тема', 'темы', 'тем')}` : '';

  iv.themes.hidden = !ivCustom;
  iv.themes.innerHTML = '';
  if (bank && ivCustom) {
    const max = ivCurrentLevel() ? ivCurrentLevel().max_level : 99;
    for (const name of bank.categories) {
      const total = bank.questions.filter((q) => q.category === name && q.level <= max).length;
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (ivChosen.has(name) ? ' on' : '') + (total ? '' : ' empty');
      chip.setAttribute('aria-pressed', String(ivChosen.has(name)));
      chip.innerHTML = '<span></span><span class="n"></span>';
      chip.children[0].textContent = name;
      chip.children[1].textContent = total;
      chip.title = total ? `вопросов по теме: ${total}` : 'на этой строгости вопросов по теме нет';
      chip.addEventListener('click', () => {
        if (ivChosen.has(name)) ivChosen.delete(name);
        else ivChosen.add(name);
        ivRenderThemes();
      });
      iv.themes.appendChild(chip);
    }
  }
  ivUpdateStart();
}

iv.presets.querySelectorAll('button').forEach((b) => {
  b.addEventListener('click', () => {
    ivCustom = b.dataset.preset === 'custom';
    if (!ivCustom) ivChosen = ivSet(b.dataset.preset);
    ivRenderThemes();
  });
});

// --- строгость ---

function ivRenderLevels() {
  iv.levels.innerHTML = '';
  for (const l of ivLevels) {
    const on = l.id === ivLevel;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = on ? 'on' : '';
    btn.setAttribute('role', 'radio');
    btn.setAttribute('aria-checked', String(on));
    btn.textContent = l.name[0].toUpperCase() + l.name.slice(1);
    btn.addEventListener('click', () => {
      ivLevel = l.id;
      ivRenderLevels();
      ivRenderThemes();
    });
    iv.levels.appendChild(btn);
  }
  const level = ivCurrentLevel();
  iv.levelDesc.textContent = level ? level.hint : '';
}

function ivUpdateStart() {
  const picked = ivPicked();
  const n = picked.length;
  const level = ivCurrentLevel();
  if (!ivBanks.length) {
    $('ivSumCount').textContent = 'Банков пока нет';
    $('ivSumMeta').textContent = 'Сделай свой банк в первой строке';
    iv.start.disabled = true;
    return;
  }
  $('ivSumCount').textContent = n ? count(n, 'вопрос', 'вопроса', 'вопросов') : 'Вопросов не осталось';
  $('ivSumMeta').textContent = n
    ? `Строгость «${level ? level.name : ''}», ${ivEstimate(picked)}`
    : 'Сними лишние темы или выбери строгость помягче';
  iv.start.disabled = n === 0;
}

async function ivLoadSetup() {
  const s = await api().interview_setup();
  ivLevels = s.levels || [];
  ivBanks = s.banks || [];
  ivSavedThemes = s.saved_categories || {};
  if (!ivLevels.some((l) => l.id === ivLevel)) ivLevel = ivLevels.length ? ivLevels[0].id : 'рабочий';
  ivRenderLevels();
  ivRestoreThemes();

  const d = await api().get_devices();
  const pick = iv.mic.value !== '' ? iv.mic.value : mt.mic.value;
  fillSelect(iv.mic, d.mics, pick !== '' ? Number(pick) : d.default_mic);
  checkSamples = [];
  drawLive($('waveCheck'), checkSamples);

  iv.history.innerHTML = '';
  iv.historyCard.hidden = !(s.history && s.history.length);
  for (const h of s.history || []) {
    const row = document.createElement('div');
    row.innerHTML = '<span></span><span></span>';
    row.children[0].textContent = `${h.дата}, ${h.банк}, ${count(h.вопросов, 'вопрос', 'вопроса', 'вопросов')}`;
    row.children[1].textContent = `${h.заминок_на_ответ} слов-паразитов на ответ`;
    iv.history.appendChild(row);
  }
}

$('ivCopyBankPrompt').addEventListener('click', () => {
  copyAndSay(BANK_PROMPT, 'Запрос скопирован. Допиши вакансию и вставь в чат с нейросетью');
});
$('ivBanksDir').addEventListener('click', () => api().open_banks_dir());

// --- проверка микрофона до начала ---

$('ivCheckMic').addEventListener('click', async () => {
  const btn = $('ivCheckMic');
  const info = $('ivCheckText');

  if (micChecking) {
    const r = await api().mic_check_stop();
    micChecking = false;
    btn.textContent = 'Проверить';
    if (r.error) { info.textContent = r.error; info.dataset.kind = 'bad'; return; }
    const ok = r.verdict === 'хорошо';
    info.dataset.kind = ok ? 'good' : 'bad';
    info.title = `пик ${r.peak}`;
    info.textContent = ok
      ? 'Слышу хорошо, можно начинать.'
      : 'Слишком тихо, ответы не запишутся. Выбери другой микрофон или подними усиление в настройках.';
    return;
  }

  const r = await api().mic_check_start(Number(iv.mic.value), deviceName(iv.mic));
  if (r.error) { info.textContent = r.error; info.dataset.kind = 'bad'; return; }
  micChecking = true;
  checkSamples = [];
  info.dataset.kind = '';
  btn.textContent = 'Готово';
  info.textContent = 'Говори обычным голосом…';
});

// --- сессия ---

iv.start.addEventListener('click', async () => {
  if (ivBusy) return;
  if (micChecking) $('ivCheckMic').click();
  ivBusy = true;
  iv.start.disabled = true;
  const r = await api().interview_start(
    ivBankFile, ivLevel, Number(iv.mic.value), deviceName(iv.mic), [...ivChosen], $('ivShuffle').checked,
  );
  ivBusy = false;
  iv.start.disabled = false;
  if (r.error) { toast(r.error, 5000); return; }
  vt(() => ivRenderQuestion(r), 'question');
});

function ivPaintPips(p) {
  iv.pips.innerHTML = '';
  for (let i = 1; i <= p.total; i++) {
    const pip = document.createElement('i');
    pip.className = i < p.index ? 'done' : i === p.index ? 'now' : '';
    iv.pips.appendChild(pip);
  }
}

function ivRenderQuestion(payload) {
  if (payload.finished) { ivFinish(); return; }
  const q = payload.question;
  const p = payload.progress;
  ivTheses = q.theses || [];
  ivQuestionNo = p.index;
  iv.progress.textContent = `Вопрос ${p.index} из ${p.total}`;
  ivPaintPips(p);
  iv.category.textContent = q.category || '';
  iv.question.textContent = q.text;

  ivTarget = q.target;
  const bits = [];
  if (q.target) bits.push(`Уложись в ${q.target[0]}–${q.target[1]} секунд.`);
  if (q.fillers_limit !== null && q.fillers_limit !== undefined) {
    bits.push(`Слов-паразитов не больше ${q.fillers_limit}.`);
  }
  iv.target.textContent = bits.join(' ');
  ivPendingFollowup = '';
  ivNewTake();
  ivShow(iv.ask);
  ivDrawTake();
}

function ivNewTake() {
  const max = ivTarget ? Math.max(ivTarget[1] * 1.4, ivTarget[1] + 20) : 90;
  ivTake = { samples: [], maxSec: Math.round(max), target: ivTarget };
  iv.timer.textContent = '0:00';
  iv.timer.classList.remove('over');
  iv.answer.setAttribute('aria-label', 'Записать ответ');
  iv.answerLabel.textContent = 'Записать ответ';
  iv.hint.textContent = IV_IDLE_HINT;
  iv.skip.hidden = !!ivPendingFollowup;
  paintScale(iv.takeScale, ivTake.maxSec, ivTake.target);
}

function ivDrawTake() {
  drawTake(iv.waveTake, ivTake.samples, ivTake.maxSec, ivTake.target);
}

function ivTakeTick() {
  ivTake.samples.push(lastLevels[0]);
  const passed = (Date.now() - ivStarted) / 1000;
  // Ответ вылез за шкалу: растягиваем её, а не обрезаем дорожку
  if (passed > ivTake.maxSec * 0.95) {
    ivTake.maxSec = Math.round(passed * 1.25);
    paintScale(iv.takeScale, ivTake.maxSec, ivTake.target);
  }
  iv.timer.textContent = clockText(passed);
  iv.timer.classList.toggle('over', !!ivTarget && passed > ivTarget[1]);
  ivDrawTake();
}

async function ivToggleAnswer() {
  if (ivBusy) return;
  ivBusy = true;
  iv.answer.disabled = true;

  if (!ivAnswering) {
    const r = ivPendingFollowup
      ? await api().followup_start(ivPendingFollowup)
      : await api().answer_start();
    ivBusy = false;
    iv.answer.disabled = false;
    if (r.error) { toast(r.error, 5000); return; }
    ivTake.samples = [];
    lastLevels = [0, 0];
    ivAnswering = true;
    document.body.classList.add('answering');
    iv.answer.setAttribute('aria-label', 'Закончить ответ');
    iv.answerLabel.textContent = 'Закончить ответ';
    iv.hint.textContent = 'Говори. Когда закончишь, нажми кнопку ещё раз или пробел.';
    iv.skip.disabled = iv.finish.disabled = true;
    ivStarted = Date.now();
    return;
  }

  ivAnswering = false;
  document.body.classList.remove('answering');
  ivLastTake = { ...ivTake, samples: ivTake.samples.slice() };
  iv.answerLabel.textContent = 'Расшифровываю…';
  iv.hint.textContent = '';
  iv.skip.disabled = iv.finish.disabled = false;
  // Пока идёт расшифровка, по дорожке бежит блик: программа работает, а не зависла
  const timeline = iv.waveTake.parentElement;
  timeline.classList.add('busy');

  const r = await api().answer_stop();
  timeline.classList.remove('busy');
  ivBusy = false;
  iv.answer.disabled = false;
  const wasFollowup = !!ivPendingFollowup;
  ivPendingFollowup = '';
  if (r.error) { ivNewTake(); toast(r.error, 5000); ivDrawTake(); return; }
  // Дорожка дубля переезжает в разбор, а не пропадает вместе с экраном
  vt(() => {
    ivNewTake();
    if (r.empty) ivRenderEmpty(r);
    else ivRenderResult(r, wasFollowup);
  }, 'review');
}

iv.answer.addEventListener('click', ivToggleAnswer);

// Пробелом удобнее, чем целиться мышкой, пока говоришь
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || screen !== 'interview' || iv.ask.hidden || $('dlg').open) return;
  const tag = document.activeElement?.tagName;
  if (['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY'].includes(tag)) return;
  e.preventDefault();
  if (!e.repeat) ivToggleAnswer();
});

// В разборе Enter жмёт главную кнопку, R даёт ещё дубль
document.addEventListener('keydown', (e) => {
  if (screen !== 'interview' || iv.result.hidden || typing() || $('dlg').open || e.repeat) return;
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  if (e.key === 'Enter' && document.activeElement?.tagName !== 'BUTTON') {
    const main = iv.result.querySelector('.btn.primary:not([hidden])');
    if (main && !main.closest('[hidden]')) { e.preventDefault(); main.click(); }
  }
  if (e.code === 'KeyR' && !iv.retry.hidden) { e.preventDefault(); iv.retry.click(); }
});

/** Дубль-уточнение: тот же экран, другой вопрос, без окна цели. */
function ivAskFollowup(text) {
  ivPendingFollowup = text;
  iv.question.textContent = text;
  iv.category.textContent = 'уточнение к ответу';
  iv.target.textContent = 'Скажи коротко и по делу, чего не хватило.';
  ivTarget = null;
  ivNewTake();
  ivShow(iv.ask);
  ivDrawTake();
}

/** Конкретная причина вместо общего «можно лучше»: видно, что чинить. */
function ivVerdictTitle(v) {
  if (v.clean) return 'Чистый ответ';
  const missing = (v.covered || []).filter((ok) => !ok).length;
  if (missing) return `Не прозвучало ${missing} из ${v.covered.length}`;
  if (v.fillers_limit !== null && v.fillers_limit !== undefined && v.fillers > v.fillers_limit) {
    return 'Много слов-паразитов';
  }
  if (v.time_verdict === 'долго') return 'Слишком долго';
  if (v.time_verdict === 'коротко') return 'Слишком коротко';
  return 'Можно чище';
}

function ivPaintVerdict(kind, title, sub) {
  iv.verdict.dataset.kind = kind;
  iv.verdictIcon.setAttribute('href', kind === 'good' ? '#i-check' : '#i-alert');
  iv.clean.textContent = title;
  iv.attempt.textContent = sub;
}

function ivMeasure(label, value, note, good) {
  const m = document.createElement('div');
  m.className = 'measure ' + (good ? 'good' : 'warn');
  m.innerHTML = '<small></small><b></b><span></span>';
  m.children[0].textContent = label;
  m.children[1].textContent = value;
  m.children[2].textContent = note;
  return m;
}

/** Главная кнопка одна, остальные обычные. На главной подсказка: её жмёт Enter. */
function ivPrimary(button) {
  for (const b of [iv.retry, iv.next, $('ivFollowupAnswer')]) {
    b.className = 'btn' + (b === button ? ' primary' : '');
    b.querySelector('.kbd-enter')?.remove();
  }
  button.insertAdjacentHTML('beforeend', '<kbd class="kbd-enter">Enter</kbd>');
}

function ivShowResultTake() {
  const take = ivLastTake || { samples: [], maxSec: 90, target: null };
  ivShow(iv.result);
  paintScale($('resultScale'), take.maxSec, take.target);
  drawTake($('waveResult'), take.samples, take.maxSec, take.target);
}

function ivRenderResult(r, wasFollowup) {
  const v = r.verdict;
  ivPaintVerdict(v.clean ? 'good' : 'warn', ivVerdictTitle(v),
    `Вопрос ${ivQuestionNo}, дубль ${v.attempt}` + (wasFollowup || v.after_followup ? ', после уточнения' : ''));

  // движение внутри сессии и движение между днями показываем вместе
  const notes = [v.delta, v.versus_past].filter(Boolean);
  iv.delta.hidden = !notes.length;
  iv.delta.textContent = notes.join('. ').replace(/ · /g, ', ');

  iv.rows.innerHTML = '';
  const timeOk = v.time_verdict === 'в норме' || !v.target;
  iv.rows.appendChild(ivMeasure('Время', `${v.duration} с`,
    v.target ? `цель ${v.target[0]}–${v.target[1]} с` + (timeOk ? '' : `, ${v.time_verdict}`) : 'без цели',
    timeOk));
  const hasLimit = v.fillers_limit !== null && v.fillers_limit !== undefined;
  iv.rows.appendChild(ivMeasure('Слова-паразиты', String(v.fillers),
    hasLimit ? `можно не больше ${v.fillers_limit}` : 'чем меньше, тем лучше',
    !hasLimit || v.fillers <= v.fillers_limit));
  iv.rows.appendChild(ivMeasure('Неуверенные слова', String(v.hedges),
    v.hedges ? 'вроде «наверное» и «если честно»' : 'ни одного', v.hedges === 0));

  // тезисы: что прозвучало, а что нет
  iv.theses.innerHTML = '';
  iv.theses.hidden = !(v.covered && v.covered.length);
  if (!iv.theses.hidden) {
    iv.theses.innerHTML = '<h3>Что нужно было сказать</h3>';
    v.covered.forEach((ok, i) => {
      const line = document.createElement('div');
      line.className = 'thesis ' + (ok ? 'ok' : 'no');
      // Номер нужен, чтобы тезисы отмечались по очереди, а не все разом
      line.style.setProperty('--i', i);
      line.innerHTML = `<span class="mark" aria-label="${ok ? 'прозвучало' : 'не прозвучало'}">`
        + `${icon(ok ? 'check' : 'x')}</span><span></span>`;
      line.lastElementChild.textContent = ivTheses[i] || '';
      iv.theses.appendChild(line);
    });
  }

  // что закрылось именно благодаря уточнению
  iv.closed.hidden = true;
  if (v.just_closed && v.just_closed.length) {
    iv.closed.hidden = false;
    iv.closed.className = 'note good';
    iv.closed.textContent = `Засчитано после уточнения: ${v.just_closed.join(', ')}.`;
  } else if (v.after_followup) {
    iv.closed.hidden = false;
    iv.closed.className = 'note';
    iv.closed.textContent = 'Уточнение не помогло, нужное так и не прозвучало.';
  }

  iv.followup.hidden = !v.followup;
  if (v.followup) {
    $('ivFollowupText').textContent = v.followup;
    $('ivFollowupAnswer').onclick = () => vt(() => ivAskFollowup(v.followup), 'question');
    // Уточняющий вопрос выезжает после того, как отметились все тезисы
    iv.followup.style.setProperty('--i', (v.covered || []).length);
  }

  iv.text.textContent = r.text;
  iv.retry.hidden = !v.can_retry;
  // Есть уточнение, значит отвечать на него. Ответ просел, значит ещё дубль. Иначе дальше
  if (v.followup) ivPrimary($('ivFollowupAnswer'));
  else ivPrimary(v.can_retry && v.should_retry ? iv.retry : iv.next);
  ivShowResultTake();
}

// Пустой ответ нельзя показать мимолётным сообщением: он значит, что микрофон
// не пишет, и без явного экрана легко прокликать всю тренировку впустую
function ivRenderEmpty(r) {
  ivPaintVerdict('warn', r.was_followup ? 'Уточнение не записано' : 'Ответ не записан', 'Проверь микрофон');
  iv.delta.hidden = true;
  iv.closed.hidden = true;
  iv.theses.hidden = true;
  iv.followup.hidden = true;
  iv.rows.innerHTML = '';
  iv.rows.appendChild(ivMeasure('Длительность', `${r.duration} с`, 'распознать нечего', false));
  iv.text.textContent = 'Программа не услышала речи. Скорее всего выбран не тот микрофон или он '
    + 'выключен в настройках звука Windows. Вернись к выбору и нажми «Проверить» рядом с микрофоном.';
  iv.text.closest('details').open = true;
  iv.retry.hidden = false;
  ivPrimary(iv.retry);
  ivShowResultTake();
}

/**
 * Время ответа на шкале с окном цели. Длина полосы ровно равна секундам
 * на той же шкале, что и серое окно, поэтому видно, попал ли ответ в цель.
 */
function ivTimeBar(row) {
  const t = row.target;
  const d = row.duration;
  const max = Math.max(t ? t[1] * 1.4 : 0, d * 1.1, 10);
  const pct = (x) => `${(x / max) * 100}%`;
  const box = document.createElement('span');
  box.className = 'tbar';
  box.title = t ? `${d} с при цели ${t[0]}–${t[1]} с` : `${d} с, без цели`;
  if (t) {
    const win = document.createElement('i');
    win.style.left = pct(t[0]);
    win.style.width = pct(t[1] - t[0]);
    box.appendChild(win);
  }
  const fill = document.createElement('b');
  fill.style.width = pct(d);
  fill.className = !t ? '' : d > t[1] ? 'over' : d < t[0] ? 'short' : 'ok';
  box.appendChild(fill);
  return box;
}

function ivSummaryRow(row, i) {
  const line = document.createElement('div');
  line.className = 'sum-row' + (row.skipped ? ' skipped' : row.clean ? ' clean' : ' weak');
  line.style.setProperty('--i', i);
  const mark = row.skipped ? '' : icon(row.clean ? 'check' : 'alert');
  line.innerHTML = `<span class="mark">${mark}</span><span class="q"><b></b><small></small></span>`;
  line.querySelector('b').textContent = row.text;
  line.querySelector('b').title = row.text;
  const takes = row.takes > 1 ? `, ${count(row.takes, 'дубль', 'дубля', 'дублей')}` : '';
  line.querySelector('small').textContent = row.category + takes;
  if (row.skipped) {
    const s = document.createElement('span');
    s.className = 'skip-note';
    s.textContent = 'пропущен';
    line.appendChild(s);
    return line;
  }
  line.appendChild(ivTimeBar(row));
  const cells = [
    [`${row.duration} с`, row.target && (row.duration < row.target[0] || row.duration > row.target[1])],
    [`${row.covered} из ${row.total}`, row.covered < row.total],
    [String(row.fillers), row.fillers_limit !== null && row.fillers > row.fillers_limit],
  ];
  for (const [text, bad] of cells) {
    const c = document.createElement('span');
    c.className = 'num' + (bad ? ' bad' : '');
    c.textContent = text;
    line.appendChild(c);
  }
  return line;
}

let ivWeakIds = [];

async function ivFinish() {
  const r = await api().interview_finish();
  if (r.error) { toast(r.error); return; }
  ivPrompt = r.prompt || '';
  ivPath = r.path || '';
  const saved = !!r.path;
  const sum = r.summary || { rows: [], answered: 0, takes: 0, clean: 0, weak_ids: [] };
  ivWeakIds = sum.weak_ids || [];

  vt(() => {
    $('ivDoneTitle').textContent = !saved ? 'Записывать было нечего'
      : ivWeakIds.length ? 'Тренировка закончена' : 'Все ответы чистые';
    $('ivDoneText').textContent = saved
      ? `${count(sum.answered, 'вопрос', 'вопроса', 'вопросов')}, ${count(sum.takes, 'дубль', 'дубля', 'дублей')}. `
        + `Чисто с последнего дубля: ${sum.clean} из ${sum.answered}.`
      : 'Ни один ответ не записался. Если ты отвечал вслух, микрофон не пишет. '
        + 'Проверь, что выбран нужный вход и что он включён в настройках звука Windows.';

    const rows = $('ivSumRows');
    rows.innerHTML = '';
    sum.rows.forEach((row, i) => rows.appendChild(ivSummaryRow(row, i)));
    $('ivSummary').hidden = !saved || !sum.rows.length;

    $('ivReview').hidden = !saved;
    $('ivCopyPath').hidden = !saved;
    $('ivWeak').hidden = !ivWeakIds.length;
    $('ivWeakText').textContent = `Прогнать слабые ещё раз, ${count(ivWeakIds.length, 'вопрос', 'вопроса', 'вопросов')}`;
    $('ivCopyPrompt').className = 'btn big' + (ivWeakIds.length ? '' : ' primary');
    ivShow(iv.done);
  }, 'question');
}

async function ivGo(call) {
  const r = await call();
  if (r.error) { toast(r.error); return; }
  vt(() => ivRenderQuestion(r), 'question');
}

// Тот же банк и та же строгость, но только вопросы, где последний дубль не чистый
$('ivWeak').addEventListener('click', async () => {
  if (!ivWeakIds.length || ivBusy) return;
  ivBusy = true;
  const r = await api().interview_start(
    ivBankFile, ivLevel, Number(iv.mic.value), deviceName(iv.mic), [], false, ivWeakIds,
  );
  ivBusy = false;
  if (r.error) { toast(r.error, 5000); return; }
  vt(() => ivRenderQuestion(r), 'question');
});

iv.skip.addEventListener('click', () => ivGo(() => api().interview_next()));
iv.next.addEventListener('click', () => ivGo(() => api().interview_next()));
iv.retry.addEventListener('click', () => ivGo(() => api().interview_retry()));
iv.finish.addEventListener('click', ivFinish);
iv.finish2.addEventListener('click', ivFinish);

$('ivCopyPrompt').addEventListener('click', () => {
  if (!ivPrompt) { toast('Разбирать нечего'); return; }
  copyAndSay(ivPrompt, 'Скопировано, вставь в чат с нейросетью');
});

$('ivCopyPath').addEventListener('click', () => {
  if (ivPath) copyAndSay(ivPath, 'Путь скопирован');
});

$('ivAgain').addEventListener('click', async () => {
  await ivLoadSetup();
  vt(() => ivShow(iv.setup), 'question');
});

// ================================================================= архив

const ar = {
  search: $('arSearch'), list: $('arList'), empty: $('arEmpty'), view: $('arView'),
  name: $('arName'), meta: $('arMeta'), body: $('arBody'), digestBox: $('arDigestBox'),
  card: $('arCard'), cardTitle: $('arCardTitle'), fields: $('arFields'),
  notes: $('arNotes'), tail: $('arTail'), more: $('arMore'), menu: $('arMenu'),
};

// Имя, которое программа даёт файлу сама: «2026-10-01_16-04 расшифровка».
// В списке оно читается как служебное, а дата и так стоит строкой ниже
const AR_DEFAULT_NAME = /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}( расшифровка)?$/;
const AR_KIND = { 'встреча': 'Встреча', 'мок-собес': 'Тренировка' };
const arTitle = (name) => (AR_DEFAULT_NAME.test(name) ? 'Без названия' : name);

let arItems = [];
let arKind = 'все';
let arCurrent = null;
let arSearchTimer = null;

/** «00:25:22» → «25 мин», «02:59:01» → «2 ч 59 мин» */
function shortDuration(d) {
  const m = /^(\d+):(\d{2}):(\d{2})$/.exec(d || '');
  if (!m) return d || '';
  const h = Number(m[1]);
  const min = Number(m[2]);
  const sec = Number(m[3]);
  if (h) return `${h} ч ${min} мин`;
  if (min) return `${min} мин`;
  return `${sec} с`;
}

/** «Встреча, 01.10.2026 16:30, 173 реплики, 00:25:22» для шапки записи */
function arDescribe(it) {
  const bits = [AR_KIND[it.kind] || it.kind, it.when];
  if (it.replicas) bits.push(count(it.replicas, 'реплика', 'реплики', 'реплик'));
  if (it.duration) bits.push(it.duration);
  return bits.filter(Boolean).join(', ');
}

/** «01.10.2026 16:30, 25 мин» для списка: тип и так виден по фильтру */
function arShort(it) {
  const bits = [it.kind === 'мок-собес' ? 'Тренировка' : '', it.when, shortDuration(it.duration)];
  return bits.filter(Boolean).join(', ');
}

function arRender() {
  const items = arKind === 'все' ? arItems : arItems.filter((i) => i.kind === arKind);
  ar.list.innerHTML = '';
  if (!items.length) {
    const p = document.createElement('p');
    p.className = 'empty-note';
    p.textContent = ar.search.value
      ? 'Ничего не нашлось'
      : arItems.length ? 'В этой категории пусто' : 'Записей пока нет';
    ar.list.appendChild(p);
    return;
  }
  for (const it of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ar-item' + (arCurrent === it.path ? ' on' : '');
    btn.dataset.path = it.path;
    btn.innerHTML = '<span class="nm"></span><span class="sub"></span><div class="prev"></div>';
    const nm = btn.querySelector('.nm');
    nm.textContent = arTitle(it.name);
    nm.classList.toggle('untitled', AR_DEFAULT_NAME.test(it.name));
    btn.querySelector('.sub').textContent = arShort(it);
    btn.querySelector('.prev').textContent = it.preview || '';
    if (it.hits && it.hits.length) {
      const h = document.createElement('div');
      h.className = 'hit';
      h.textContent = it.hits[0];
      btn.appendChild(h);
    }
    btn.addEventListener('click', () => arOpen(it.path));
    ar.list.appendChild(btn);
  }
}

async function arLoad(query) {
  arItems = query ? await api().archive_search(query) : await api().archive_list();
  arRender();
}

/** Шапка файла остаётся в тексте как была, а в окне подписи и значения идут строкой. */
function arPaintHead(meta) {
  const m = meta || {};
  const fields = m.fields || [];
  const notes = (m.notes || []).join('\n\n');
  const has = !!(m.title || fields.length || notes);
  ar.card.hidden = !has;
  if (!has) return;
  ar.cardTitle.hidden = !m.title;
  ar.cardTitle.textContent = m.title || '';
  ar.fields.innerHTML = '';
  for (const [key, value] of fields) {
    const box = document.createElement('div');
    box.innerHTML = '<div class="f-key"></div><div class="f-val"></div>';
    box.children[0].textContent = key;
    // значение бывает с путями в обратных кавычках, это markdown
    box.children[1].innerHTML = window.md.inline(value);
    ar.fields.appendChild(box);
  }
  ar.notes.hidden = !notes;
  ar.notes.innerHTML = notes ? window.md.render(notes) : '';
}

async function arOpen(path) {
  const data = await api().archive_read(path);
  if (data.error) { toast(data.error); return; }
  arCurrent = path;
  ar.empty.hidden = true;
  ar.view.hidden = false;
  ar.digestBox.hidden = true;
  ar.name.textContent = arTitle(data.name);
  const item = arItems.find((i) => i.path === path);
  ar.meta.textContent = item ? arDescribe(item) : path;
  arPaintHead(data.meta);

  ar.body.innerHTML = '';
  ar.tail.hidden = true;
  ar.tail.innerHTML = '';
  if (data.replicas.length) {
    // стенограмма в том же виде, что и во время записи
    let prev = null;
    for (const r of data.replicas) {
      ar.body.appendChild(makeUtt({ who: r.who, speaker: r.speaker, time: shortTime(r.time), text: r.text, prev }));
      prev = r.who;
    }
    // всё, что дописано после реплик: разбор речи, выжимка
    if ((data.tail || '').trim()) {
      ar.tail.hidden = false;
      ar.tail.innerHTML = window.md.render(data.tail);
    }
  } else {
    // разбор тренировки и расшифровки, набранные не программой
    const box = document.createElement('div');
    box.className = 'md';
    box.innerHTML = window.md.render(data.body || data.text);
    ar.body.appendChild(box);
  }
  ar.view.parentElement.scrollTop = 0;
  replay(ar.view);
  arRender();
  arHighlight(ar.search.value);
}

// --- найденное в тексте записи ---

let arMarks = [];
let arMarkAt = -1;

/** «ё» и «е» в поиске одно и то же: распознавание пишет то так, то так. */
function arPattern(query) {
  const safe = query.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return safe ? new RegExp(safe.replace(/[её]/gi, '[её]'), 'gi') : null;
}

/** Подсвечивает слово из поиска в открытой записи и встаёт на первое совпадение. */
function arHighlight(query) {
  for (const m of ar.view.querySelectorAll('mark')) m.replaceWith(m.textContent);
  ar.view.normalize();
  arMarks = [];
  arMarkAt = -1;
  const rx = arPattern(query);
  $('arFind').hidden = !rx;
  if (!rx) return;

  const walker = document.createTreeWalker(ar.view, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest('.ar-body, .ar-tail, .ar-digest')
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    rx.lastIndex = 0;
    if (!rx.test(node.data)) continue;
    rx.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const hit of node.data.matchAll(rx)) {
      frag.append(node.data.slice(last, hit.index));
      const mark = document.createElement('mark');
      mark.textContent = hit[0];
      frag.append(mark);
      arMarks.push(mark);
      last = hit.index + hit[0].length;
    }
    frag.append(node.data.slice(last));
    node.replaceWith(frag);
  }
  if (arMarks.length) arFindGo(1);
  else $('arFindText').textContent = 'В тексте не нашлось';
  $('arFindPrev').disabled = $('arFindNext').disabled = arMarks.length < 2;
}

function arFindGo(step) {
  if (!arMarks.length) return;
  arMarks[arMarkAt]?.classList.remove('now');
  arMarkAt = (arMarkAt + step + arMarks.length) % arMarks.length;
  const mark = arMarks[arMarkAt];
  mark.classList.add('now');
  mark.scrollIntoView({ block: 'center', behavior: calm.matches ? 'auto' : 'smooth' });
  $('arFindText').textContent = `${arMarkAt + 1} из ${arMarks.length}`;
}

$('arFindPrev').addEventListener('click', () => arFindGo(-1));
$('arFindNext').addEventListener('click', () => arFindGo(1));

// Enter в поиске ходит по совпадениям в открытой записи, стрелки по списку записей
ar.search.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && arMarks.length) { e.preventDefault(); arFindGo(e.shiftKey ? -1 : 1); }
});

document.addEventListener('keydown', (e) => {
  if (screen !== 'archive' || openMenu() || $('dlg').open) return;
  if (e.key === 'F3') { e.preventDefault(); arFindGo(e.shiftKey ? -1 : 1); return; }
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  const inList = ar.list.contains(document.activeElement) || document.activeElement === ar.search;
  if (!inList) return;
  const items = [...ar.list.querySelectorAll('.ar-item')];
  if (!items.length) return;
  e.preventDefault();
  const at = items.findIndex((b) => b.classList.contains('on'));
  const next = items[Math.min(items.length - 1, Math.max(0, at + (e.key === 'ArrowDown' ? 1 : -1)))];
  // список перерисуется, поэтому фокус ставим на новую кнопку, когда запись откроется
  arOpen(next.dataset.path).then(() => {
    const on = ar.list.querySelector('.ar-item.on');
    on?.focus();
    on?.scrollIntoView({ block: 'nearest' });
  });
});

function arShowDigest(d) {
  const box = ar.digestBox;
  box.hidden = false;
  box.innerHTML = '<h3>Выжимка</h3>';
  const words = d.by_speaker || {};
  const total = Object.values(words).reduce((a, b) => a + b, 0) || 1;
  const share = Object.entries(words)
    .sort((a, b) => b[1] - a[1])
    .map(([who, n]) => `${who} ${Math.round((n * 100) / total)}%`)
    .join(', ');
  const head = document.createElement('p');
  head.className = 'help';
  head.textContent = count(d.replicas, 'реплика', 'реплики', 'реплик')
    + (d.duration ? `, длительность ${d.duration}` : '') + (share ? `. Доля слов: ${share}` : '');
  box.appendChild(head);

  const sections = d.sections || {};
  if (!Object.keys(sections).length) {
    const p = document.createElement('p');
    p.className = 'help';
    p.style.marginTop = '10px';
    p.textContent = 'Ничего похожего на договорённости и задачи не нашлось.';
    box.appendChild(p);
    return;
  }
  for (const [name, items] of Object.entries(sections)) {
    const sec = document.createElement('div');
    sec.className = 'sec';
    const b = document.createElement('b');
    b.textContent = name;
    sec.appendChild(b);
    for (const it of items) {
      const row = document.createElement('div');
      row.textContent = it;
      sec.appendChild(row);
    }
    box.appendChild(sec);
  }
}

ar.search.addEventListener('input', () => {
  clearTimeout(arSearchTimer);
  arSearchTimer = setTimeout(async () => {
    await arLoad(ar.search.value);
    if (arCurrent) arHighlight(ar.search.value);
  }, 250);
});

document.querySelectorAll('.ar-filters button').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.ar-filters button').forEach((x) => x.classList.toggle('on', x === b));
    arKind = b.dataset.kind;
    arRender();
  });
});

$('arDigest').addEventListener('click', async () => {
  if (!arCurrent) return;
  const r = await api().archive_digest(arCurrent);
  if (r.error) { toast(r.error); return; }
  arShowDigest(r.digest);
  toast('Выжимка готова и дописана в файл');
});

$('arCopyText').addEventListener('click', async () => {
  const data = await api().archive_read(arCurrent);
  if (data.error) { toast(data.error); return; }
  copyAndSay(data.text, 'Текст скопирован');
});

// --- меню «ещё» ---

/** Закрывает любое открытое меню: «ещё» в архиве и выбор устройств. */
function closeMenu() {
  for (const menu of document.querySelectorAll('.menu')) menu.hidden = true;
  for (const b of document.querySelectorAll('[aria-expanded="true"]')) b.setAttribute('aria-expanded', 'false');
}

const openMenu = () => document.querySelector('.menu:not([hidden])');

ar.more.addEventListener('click', (e) => {
  e.stopPropagation();
  const open = ar.menu.hidden;
  closeMenu();
  ar.menu.hidden = !open;
  ar.more.setAttribute('aria-expanded', String(open));
  if (open) ar.menu.querySelector('button').focus();
});

document.addEventListener('click', (e) => {
  const menu = openMenu();
  if (menu && !menu.contains(e.target)) closeMenu();
});

// В открытом меню стрелки ходят по пунктам, Escape закрывает и возвращает фокус
document.addEventListener('keydown', (e) => {
  const menu = openMenu();
  if (!menu) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    const owner = menu.previousElementSibling;
    closeMenu();
    owner?.focus();
    return;
  }
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  if (menu.classList.contains('gain-pop')) return;
  e.preventDefault();
  const items = [...menu.querySelectorAll('button')];
  const at = items.indexOf(document.activeElement);
  const next = e.key === 'ArrowDown' ? at + 1 : at - 1;
  items[(next + items.length) % items.length].focus();
});

$('arCopyPath').addEventListener('click', () => {
  closeMenu();
  copyAndSay(arCurrent, 'Путь скопирован');
});

$('arOpen').addEventListener('click', async () => {
  closeMenu();
  const r = await api().archive_open(arCurrent);
  if (r.error) toast(r.error);
});

$('arRename').addEventListener('click', async () => {
  closeMenu();
  const item = arItems.find((i) => i.path === arCurrent);
  const name = await ask({
    title: 'Новое имя записи',
    input: item && !AR_DEFAULT_NAME.test(item.name) ? item.name : '',
    ok: 'Переименовать',
  });
  if (!name) return;
  const r = await api().archive_rename(arCurrent, name);
  if (r.error) { toast(r.error); return; }
  arCurrent = r.path;
  await arLoad(ar.search.value);
  await arOpen(r.path);
  toast('Переименовано');
});

$('arDelete').addEventListener('click', async () => {
  closeMenu();
  const item = arItems.find((i) => i.path === arCurrent);
  const sure = await ask({
    title: 'Удалить запись?',
    text: `«${item ? arTitle(item.name) : ''}» удалится с диска насовсем.`,
    ok: 'Удалить',
    danger: true,
  });
  if (!sure) return;
  const r = await api().archive_delete(arCurrent);
  if (r.error) { toast(r.error); return; }
  arCurrent = null;
  ar.view.hidden = true;
  ar.empty.hidden = false;
  await arLoad(ar.search.value);
  toast('Удалено');
});

// ============================================================= настройки

const st = {
  now: $('stNow'), mode: $('stMode'), modeNote: $('stModeNote'), local: $('stLocal'), api: $('stApi'),
  hardware: $('stHardware'), model: $('stModel'), modelNote: $('stModelNote'),
  provider: $('stProvider'), providerNote: $('stProviderNote'),
  urlRow: $('stUrlRow'), url: $('stUrl'),
  apiModel: $('stApiModel'), apiModels: $('stApiModels'),
  folderRow: $('stFolderRow'), folder: $('stFolder'),
  key: $('stKey'), forget: $('stForget'), keysLink: $('stKeysLink'),
  lang: $('stLang'), test: $('stTest'), result: $('stResult'),
};

const ST_MODE_NOTE = {
  local: 'Звук никуда не уходит. С видеокартой NVIDIA успевает за речью.',
  api: 'Подойдёт любой компьютер. Нужен ключ сервиса.',
};

// Что прислал питон: текущий выбор, варианты, есть ли видеокарта и ключи
let stData = null;
let stMode = 'local';

const stProvider = () => stData.providers[st.provider.value];

function stResult(text, kind) {
  st.result.textContent = text || '';
  st.result.dataset.kind = kind || '';
  paintSaveRow();
}

// Строка под формой нужна для проверки связи по API и для ошибок сохранения
function paintSaveRow() {
  document.querySelector('.save-row').hidden = stMode !== 'api' && !st.result.textContent;
}

function fillOptions(select, pairs, value) {
  select.innerHTML = '';
  for (const [id, title] of pairs) select.add(new Option(title, id));
  select.value = value;
}

async function stLoad() {
  stData = await api().asr_settings();
  const c = stData.config;
  fillOptions(st.model, stData.local_models.map((m) => [m.id, `${m.id}, ${m.size}`]), c['модель']);
  fillOptions(st.provider, Object.entries(stData.providers).map(([id, p]) => [id, p.title]), c['провайдер']);
  fillOptions(st.lang, Object.entries(stData.languages), c['язык']);
  st.url.value = c['адрес'];
  st.folder.value = c['каталог'];
  st.apiModel.value = c['модель_api'] || stProvider().models[0] || '';
  st.key.value = '';
  stMode = c['режим'];
  st.hardware.textContent = stData.cuda
    ? 'Видеокарта NVIDIA найдена, large-v3 успевает за речью.'
    : 'Видеокарты NVIDIA нет, считать будет процессор. Для живой записи бери small или распознавание через интернет.';
  st.hardware.dataset.kind = stData.cuda ? 'good' : 'warn';
  st.now.textContent = `сейчас ${stData.label}`;
  stResult('');
  stPaint();

  const s = await api().get_settings();
  paintGains(s);
}

function stPaint() {
  for (const b of st.mode.querySelectorAll('button')) {
    const on = b.dataset.mode === stMode;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  }
  st.modeNote.textContent = ST_MODE_NOTE[stMode];
  st.local.hidden = stMode !== 'local';
  st.api.hidden = stMode !== 'api';
  st.test.hidden = stMode !== 'api';
  paintSaveRow();

  const m = stData.local_models.find((x) => x.id === st.model.value);
  st.modelNote.textContent = m ? m.note : '';

  const pid = st.provider.value;
  const p = stProvider();
  st.providerNote.textContent = p.note;
  st.urlRow.hidden = pid !== 'custom';
  st.folderRow.hidden = p.kind !== 'yandex';
  st.apiModels.innerHTML = '';
  for (const id of p.models) st.apiModels.appendChild(new Option(id));
  st.apiModel.placeholder = p.models[0] || 'как её называет сервер';

  const has = stData.keys[pid];
  st.key.placeholder = has
    ? 'сохранён, вставь новый, чтобы заменить'
    : pid === 'custom' ? 'если сервер просит' : 'вставь ключ';
  st.forget.hidden = !has;
  st.keysLink.hidden = !p.keys_url;
}

function stConfig() {
  return {
    'режим': stMode,
    'модель': st.model.value,
    'язык': st.lang.value,
    'провайдер': st.provider.value,
    'модель_api': st.apiModel.value.trim(),
    'адрес': st.url.value.trim(),
    'каталог': st.folder.value.trim(),
  };
}

// --- сохранение без кнопки: что поменял, то и сохранилось ---

let stSaveTimer = null;
let stSavedTimer = null;

function stQueueSave(delay = 0) {
  clearTimeout(stSaveTimer);
  stSaveTimer = setTimeout(stSaveNow, delay);
}

/** Короткая отметка «Сохранено» в шапке, чтобы было видно, что кнопка не нужна. */
function flashSaved() {
  const s = $('stSaved');
  s.hidden = false;
  replay(s);
  clearTimeout(stSavedTimer);
  stSavedTimer = setTimeout(() => { s.hidden = true; }, 1800);
}

async function stSaveNow() {
  const key = st.key.value;
  const res = await api().asr_save(stConfig(), key);
  if (res.error) { stResult(res.error, 'bad'); return; }
  stData.keys = res.keys;
  stData.config = stConfig();
  if (key) st.key.value = '';
  st.now.textContent = `сейчас ${res.label}`;
  paintAsrLabel(res.label);
  stPaint();
  stResult('');
  flashSaved();
}

st.mode.querySelectorAll('button').forEach((b) => {
  b.addEventListener('click', () => {
    if (stMode === b.dataset.mode) return;
    stMode = b.dataset.mode;
    stResult('');
    stPaint();
    stQueueSave();
  });
});

st.model.addEventListener('change', () => { stPaint(); stQueueSave(); });
st.lang.addEventListener('change', () => stQueueSave());

st.provider.addEventListener('change', () => {
  // Модель одного сервиса другому не подходит: берём первую из его списка.
  // Ключ тоже чужой, отправлять его другому сервису нельзя
  st.apiModel.value = stProvider().models[0] || '';
  st.key.value = '';
  stResult('');
  stPaint();
  stQueueSave();
});

// Текстовые поля сохраняются, когда перестал печатать
for (const field of [st.apiModel, st.url, st.folder]) {
  field.addEventListener('input', () => stQueueSave(800));
}

// Ключ сохраняется, когда ушёл из поля или нажал Enter: по буквам его слать незачем
st.key.addEventListener('change', () => { if (st.key.value.trim()) stQueueSave(); });
st.key.addEventListener('keydown', (e) => { if (e.key === 'Enter') st.key.blur(); });

st.keysLink.addEventListener('click', () => api().open_url(stProvider().keys_url));

st.forget.addEventListener('click', async () => {
  const sure = await ask({
    title: 'Забыть ключ?',
    text: `Ключ ${stProvider().title} удалится из диспетчера учётных данных Windows.`,
    ok: 'Забыть',
    danger: true,
  });
  if (!sure) return;
  const res = await api().asr_forget_key(st.provider.value);
  if (res.error) { toast(res.error); return; }
  stData.keys = res.keys;
  stPaint();
  toast('Ключ удалён');
});

st.test.addEventListener('click', async () => {
  st.test.disabled = true;
  stResult('Проверяю…');
  const res = await api().asr_test(stConfig(), st.key.value);
  st.test.disabled = false;
  if (res.error) stResult(res.error, 'bad');
  else stResult(`Сервер отвечает, ${res.ms} мс на реплику`, 'good');
});

// --- усиление: у каждой дорожки своё, меняется прямо во время записи ---

// Один и тот же уровень живёт в двух местах: в настройках и во всплывашке
// в шапке дорожки. Двигаешь любой ползунок, второй и подписи догоняют
const gainUi = {
  me: {
    send: (v) => api().set_mic_gain(v),
    ranges: [$('gain'), $('gainMeRange')],
    labels: [$('gainValue'), $('gainMePopValue'), $('gainMeBtn')],
    chip: $('gainMeBtn'),
    pop: $('gainMePop'),
  },
  them: {
    send: (v) => api().set_them_gain(v),
    ranges: [$('gainThem'), $('gainThemRange')],
    labels: [$('gainThemValue'), $('gainThemPopValue'), $('gainThemBtn')],
    chip: $('gainThemBtn'),
    pop: $('gainThemPop'),
  },
};
const gainTimers = {};

function paintGain(which, value, min, max) {
  const g = gainUi[which];
  for (const r of g.ranges) {
    if (min !== undefined) r.min = min;
    if (max !== undefined) r.max = max;
    r.value = value;
  }
  const text = '×' + Number(value).toFixed(1);
  for (const l of g.labels) l.textContent = text;
  // Подсвечиваем, когда усиление включено: видно, что звук уже тянется
  g.chip.classList.toggle('boosted', Number(value) > 1);
  g.chip.setAttribute('aria-label', `${g.chip.title}: ${text}`);
}

function paintGains(s) {
  paintGain('me', s.gain, s.gain_min, s.gain_max);
  paintGain('them', s.them_gain ?? 1, s.gain_min, s.them_gain_max ?? s.gain_max);
}

for (const [which, g] of Object.entries(gainUi)) {
  for (const r of g.ranges) {
    r.addEventListener('input', () => {
      paintGain(which, r.value);
      // Пока тянут ползунок, событий десятки: в питон шлём только последнее
      clearTimeout(gainTimers[which]);
      gainTimers[which] = setTimeout(async () => {
        await g.send(Number(r.value));
        if (screen === 'settings') flashSaved();
      }, 120);
    });
  }
  g.chip.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = g.pop.hidden;
    closeMenu();
    if (!open) return;
    g.pop.hidden = false;
    g.chip.setAttribute('aria-expanded', 'true');
    g.pop.querySelector('input').focus();
  });
}

$('stOpenRecords').addEventListener('click', () => api().open_dir());
$('stOpenBanks').addEventListener('click', () => api().open_banks_dir());

// ================================================================ старт

setPhase('idle');
redrawIdleTracks();
paintModeMark();
// Ширина вкладок меняется, когда догружается шрифт
document.fonts.ready.then(paintModeMark);
window.addEventListener('resize', paintModeMark);

window.addEventListener('pywebviewready', async () => {
  const d = await api().get_devices();
  fillSelect(mt.mic, d.mics, d.default_mic);
  fillSelect(mt.spk, d.loopbacks, d.default_loopback);
  // Если человек в прошлый раз выбрал не то, что Windows считает основным, берём его выбор
  pickRemembered(mt.mic, 'micName');
  pickRemembered(mt.spk, 'spkName');
  const s = await api().get_settings();
  paintGains(s);
  paintAsrLabel(s.asr_label);
  if (!d.loopbacks.length) {
    paintState('Не нашёл звук собеседника', 'error', 'Проверь, что наушники подключены');
  } else {
    paintState('Готово к записи', 'idle', 'Пока не начнёшь запись, звук никуда не пишется');
  }
  setPhase('idle');
  paintHeard();
  redrawIdleTracks();
});
