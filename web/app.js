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

/**
 * Наполняет список устройств. Выбрано то, что названо в wantedName (человек уже
 * выбирал его раньше), иначе устройство по умолчанию из Windows, иначе первое.
 */
function fillSelect(select, items, defaultIndex, wantedName = '') {
  select.innerHTML = '';
  const wanted = wantedName ? items.find((d) => d.name === wantedName) : null;
  const chosen = wanted ? wanted.index : defaultIndex;
  for (const d of items) {
    const opt = new Option(prettyDevice(d.name), d.index);
    opt.title = d.name;  // полное имя по наведению, по нему же устройство ищется заново
    if (d.index === chosen) opt.selected = true;
    select.add(opt);
  }
  select.disabled = !items.length;
  if (!items.length) select.add(new Option('устройств не найдено', ''));
}

const deviceName = (select) => select.selectedOptions[0]?.title || '';
const hasDevices = (select) => select.options.length > 0 && select.options[0].value !== '';

/**
 * Номер устройства для питона. Устройства нет: -1. Number('') это 0, а 0 в Windows
 * настоящий номер («Microsoft Sound Mapper»): раньше пустой список открывал его.
 */
const devIndex = (select) => (hasDevices(select) && select.value !== '' ? Number(select.value) : -1);

// Выбор человека между запусками лежит в настройки.json, не в localStorage:
// окно работает в приватном режиме, и браузерное хранилище стирается при выходе.
// Читается при старте окна (get_settings), пишется через api().remember
let remembered = {};

function remember(key, value) {
  remembered[key] = value;
  try {
    Promise.resolve(api().remember(key, value)).catch(() => {});
  } catch { /* питон ещё не готов, выбор останется только до закрытия окна */ }
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

// Первый запуск: пока человек не выбрал, с чего начать, на экране выбор, разделы закрыты,
// а устройства никто не открывает. booted поднимается, когда окно решило, что показывать
let welcomeOn = false;
let booted = false;

// Почему в тренажёр нельзя зайти, пока запись встречи не в покое. В starting питон ещё
// не считает запись идущей (модель грузится минутами), но устройства уже заняты
const INTERVIEW_LOCKED = {
  live: 'Сначала останови запись встречи',
  starting: 'Запись запускается, подожди',
  stopping: 'Запись ещё останавливается, подожди',
};

function show(name) {
  if (name === screen) return;
  if (welcomeOn) return;  // сначала выбор «с чего начать», он ведёт в раздел сам (welcomeChoose)
  if (ivAnswering) { toast('Сначала закончи ответ'); return; }
  // Ответ расшифровывается или тренировка переходит дальше: питон ещё держит
  // микрофон и сессию, и уход отсюда оставил бы их без присмотра
  if (ivBusy) { toast('Подожди, тренажёр ещё занят'); return; }
  // Тренажёру нужен тот же микрофон, что пишет встречу
  if (name === 'interview' && INTERVIEW_LOCKED[phase]) { toast(INTERVIEW_LOCKED[phase]); return; }
  const forward = SCREENS.indexOf(name) > SCREENS.indexOf(screen);
  // Проверка микрофона живёт только на экране выбора тренировки. Саундчек после
  // неё откроется не раньше, чем она закроется (см. syncMonitor)
  if (screen === 'interview') ivDropMicCheck();
  screen = name;
  closeMenu();
  vt(() => {
    for (const b of document.querySelectorAll('[data-screen]')) {
      if (b.dataset.screen === name) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    }
    for (const s of SCREENS) $('screen-' + s).hidden = s !== name;
    $('screen-welcome').hidden = true;
    paintNavLive();
    if (name === 'meeting') redrawIdleTracks();
  }, forward ? 'next' : 'back');
  paintModeMark();
  syncMonitor();
  if (name === 'interview' && !iv.setup.hidden) ivLoadSetup();
  if (name === 'archive') arLoad(ar.search.value);
  if (name === 'settings') stLoad();
  if (name === 'meeting') refreshAsrReady();
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
}

/**
 * Строка «Распознавание» под дорожками. Галочка стоит, только когда правда готово:
 * модель скачана или ключ на месте. Иначе кружок ждёт или краснеет, а в строке
 * сказано, чего не хватает. a это то, что собрал питон: {state, label, note}.
 */
function paintAsrReady(a) {
  if (!a) return;
  $('ckAsr').dataset.state = a.state;
  $('ckAsrText').textContent = a.note ? `${a.label}, ${a.note}` : a.label;
}

/** Модель могли скачать, пока смотрели другой раздел: строка под дорожками догоняет. */
function refreshAsrReady() {
  if (!window.pywebview) return;
  try {
    Promise.resolve(api().get_settings()).then((s) => {
      paintAsrLabel(s.asr_label);
      paintAsrReady(s.asr);
      applyModel(s.model);
      applyGpu(s.gpu);
    }).catch(() => {});
  } catch { /* питон ещё не готов: строка останется как была */ }
}

// ============================================ подготовка модели и ускорение

// Модель скачивается в фоне, пока человек выбирает, с чего начать. Питон присылает ход
// (ui.modelState), окно рисует его в строке состояния того раздела, где человек сейчас:
// в записи под состоянием, в тренажёре над банками и под вопросом, в настройках под моделью.
// Установка CUDA идёт тем же компонентом (.prep) и тем же способом (ui.gpuState)
let modelInfo = { state: 'idle', done: 0, total: 0, source: 'huggingface', error: '' };
let gpuInfo = { state: 'none' };

const mb = (bytes) => Math.round(bytes / 1e6);

/** Строка для .prep: «Готовлю распознавание: 120 из 480 МБ». Запасная копия подписана. */
function modelText(m) {
  const what = m.source === 'github' ? 'Готовлю распознавание, качаю запасную копию' : 'Готовлю распознавание';
  return m.total > 0 ? `${what}: ${mb(m.done)} из ${mb(m.total)} МБ` : `${what}: подключаюсь…`;
}

/** Заготовка .prep: строка, кнопка «Попробовать снова» и тонкая полоса. */
function buildPrep(host) {
  host.innerHTML = '<div class="prep-line"><span class="prep-msg"></span>'
    + '<button type="button" class="link prep-retry" hidden>Попробовать снова</button></div>'
    + '<div class="prep-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div>';
  return host;
}

/**
 * Рисует .prep. view это null (спрятать) или {kind: 'loading' | 'error', text, pct, retry}:
 * pct число от 0 до 100 или null, если сколько осталось, пока неизвестно.
 */
function paintPrep(host, view) {
  host.hidden = !view;
  if (!view) return;
  host.dataset.kind = view.kind;
  const msg = host.querySelector('.prep-msg');
  msg.textContent = view.text;
  msg.title = view.title || '';
  const retry = host.querySelector('.prep-retry');
  retry.hidden = !view.retry;
  retry.onclick = view.retry || null;
  const fill = host.querySelector('.prep-bar i');
  host.toggleAttribute('data-wait', view.pct === null);
  fill.style.width = view.pct === null ? '' : `${view.pct}%`;
  const bar = host.querySelector('.prep-bar');
  if (view.pct === null) bar.removeAttribute('aria-valuenow');
  else bar.setAttribute('aria-valuenow', String(Math.round(view.pct)));
}

const prepHosts = () => ['prepMeeting', 'prepSetup', 'prepAsk', 'prepSettings'].map($);

function retryModel() {
  try { Promise.resolve(api().model_retry()).catch(() => {}); } catch { /* питон не отвечает */ }
}

/** Что показать про модель: идёт загрузка, сбой или ничего. */
function modelView() {
  const m = modelInfo;
  if (m.state === 'loading') {
    return {
      kind: 'loading', text: modelText(m), retry: null,
      pct: m.total > 0 ? Math.min(100, (m.done / m.total) * 100) : null,
    };
  }
  if (m.state === 'error') {
    return { kind: 'error', text: m.error || 'Распознавание не скачалось', pct: 0, retry: retryModel };
  }
  return null;
}

function paintModel() {
  const view = modelView();
  // Во время записи подпись «реплик N» важнее: полоса в окне записи уходит
  const meetingView = phase === 'live' ? null : view;
  for (const host of prepHosts()) {
    if (!host.firstElementChild) buildPrep(host);
    paintPrep(host, host.id === 'prepMeeting' ? meetingView : view);
  }
  // В строке состояния полоса занимает место подписи (style.css), поэтому состояние знает о ней
  $('state').dataset.prep = meetingView ? 'on' : 'off';
  ivPaintAnswerBtn();
}

function applyModel(info) {
  if (!info) return;
  modelInfo = info;
  paintModel();
  if (booted) paintIdleState();  // заголовок записи при сбое загрузки
}

/** Кнопка ответа в тренажёре ждёт, пока модель скачается: записывать некому. */
const ivModelBlocks = () => modelInfo.state === 'loading' || modelInfo.state === 'error';

/**
 * Видеокарта NVIDIA есть, библиотек CUDA нет: предлагаем поставить (около 2 ГБ) и
 * перезапустить программу. Состояния: offer, installing, restart, error.
 */
const boostHosts = () => ({ meeting: $('boostMeeting'), settings: $('boostSettings') });
let gpuShownSec = -1;

function gpuElapsed() {
  return gpuInfo.since ? Math.max(0, Date.now() / 1000 - gpuInfo.since) : 0;
}

function paintBoosts() {
  const g = gpuInfo;
  for (const [where, host] of Object.entries(boostHosts())) {
    if (!host.firstElementChild) {
      host.innerHTML = '<span class="boost-msg"></span><button type="button" class="btn boost-act"></button>'
        + '<div class="prep" hidden></div>';
      buildPrep(host.querySelector('.prep'));
      host.querySelector('.boost-act').addEventListener('click', gpuAct);
    }
    // Во время записи полоса в её окне не нужна, а в настройках она остаётся
    host.hidden = g.state === 'none' || (where === 'meeting' && phase === 'live');
    if (host.hidden) continue;
    host.dataset.state = g.state;
    const msg = host.querySelector('.boost-msg');
    const act = host.querySelector('.boost-act');
    const prep = host.querySelector('.prep');
    let text = '';
    let label = '';
    if (g.state === 'offer') {
      text = 'Видеокарта NVIDIA найдена, но считает пока процессор.';
      if (g.after_model) text += ` После ускорения докачается модель, около ${g.after_model}.`;
      label = `Ускорить на видеокарте, ${g.size || 'около 2 ГБ'}`;
    } else if (g.state === 'restart') {
      text = 'Библиотеки CUDA поставлены. Осталось перезапустить программу.';
      if (g.after_model) text += ` После перезапуска докачается модель, около ${g.after_model}.`;
      label = 'Перезапустить программу';
    } else if (g.state === 'error') {
      // Человеческая фраза от питона красным. Команду для ручной установки и строку самого
      // установщика (обычно английскую) показываем только там, где есть место
      text = (g.text || 'Не получилось поставить библиотеки для видеокарты').replace(/[.:!]?\s*$/, '.');
      // Нет установщика (uv): сказано, что делать, и ни подробностей, ни команды вручную нет
      if (g.uv === false) {
        // фраза от питона уже всё объяснила
      } else if (where === 'settings') {
        if (g.detail) text += ` Ответ установщика: ${g.detail.replace(/[.:!]?\s*$/, '.')}`;
        if (g.manual) text += ` Можно поставить вручную, в папке программы: ${g.manual}`;
      } else {
        text += ' Подробности в настройках.';
      }
      label = g.uv === false ? '' : 'Попробовать снова';
    }
    msg.textContent = text;
    msg.title = g.state === 'error' && g.detail ? g.detail : '';
    // В настройках про карту и перезапуск уже сказано строкой выше (stPaintHardware): повторять незачем
    msg.hidden = g.state === 'installing' || (where === 'settings' && g.state !== 'error');
    act.hidden = !label;
    act.textContent = label;
    act.classList.toggle('primary', g.state === 'restart');
    act.disabled = stLocked();
    act.title = stLocked() ? 'Сначала останови запись' : '';
    if (g.state === 'installing') {
      const sec = Math.floor(gpuElapsed());
      paintPrep(prep, {
        kind: 'loading', pct: null, retry: null, title: g.detail || '',
        text: `${g.text || 'Качаю библиотеки для видеокарты'}, прошло ${clockText(sec)}`,
      });
    } else {
      prep.hidden = true;
    }
  }
}

function applyGpu(info) {
  if (!info) return;
  gpuInfo = info;
  paintBoosts();
  if (screen === 'settings' && stData) stPaintHardware();
}

async function gpuAct() {
  if (gpuInfo.state === 'restart') { restartApp(); return; }
  for (const host of Object.values(boostHosts())) {
    const act = host.querySelector('.boost-act');
    if (act) act.disabled = true;  // двойное нажатие второй установки не запустит
  }
  let r;
  try {
    r = await api().gpu_install();
  } catch {
    r = { error: 'Программа не отвечает, установка не началась' };
  }
  if (r.error) toast(r.error, 6000);
  paintBoosts();
}

async function restartApp() {
  // Тренировка посреди: перезапуск её не сохранит, об этом надо сказать до нажатия
  const midTraining = !iv.ask.hidden || !iv.result.hidden;
  if (midTraining) {
    const sure = await ask({
      title: 'Перезапустить программу?',
      text: 'Окно закроется и откроется заново. Данные ответы незаконченной тренировки лягут в разборы.',
      ok: 'Перезапустить',
    });
    if (!sure) return;
  }
  let r;
  try {
    r = await api().restart_app();
  } catch {
    r = { error: 'Программа не отвечает, перезапуск не начался' };
  }
  if (r.error) { toast(r.error, 6000); return; }
  toast('Перезапускаю программу…', 10000);
}

// ==================================================== первый запуск

/** Экран выбора: тренажёр или запись. Пока он на месте, разделы и настройки закрыты. */
function enterWelcome() {
  welcomeOn = true;
  screen = 'welcome';
  document.body.classList.add('choosing');
  for (const s of SCREENS) $('screen-' + s).hidden = true;
  $('screen-welcome').hidden = false;
  paintModeMark();
}

/** Выбор сделан: отметка уходит в настройки.json, окно идёт в нужный раздел. */
function welcomeChoose(target) {
  if (!welcomeOn) return;
  welcomeOn = false;
  document.body.classList.remove('choosing');
  show(target);
  try {
    Promise.resolve(api().first_run_done()).catch(() => {});
  } catch { /* отметка не сохранилась: выбор покажется ещё раз, ничего страшного */ }
}

$('chooseTrainer').addEventListener('click', () => welcomeChoose('interview'));
$('chooseMeeting').addEventListener('click', () => welcomeChoose('meeting'));

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
  // Источники закрыты: до stopped питон уровней не шлёт, и дорожки не должны
  // светить последним значением
  if (p === 'stopping') lastLevels = [0, 0];
  const live = p === 'live';
  // Писать нечем, пока нет ни микрофона, ни наушников
  const noDevices = !hasDevices(mt.mic) && !hasDevices(mt.spk) && (p === 'idle' || p === 'done');
  mt.record.className = 'btn rec big';
  mt.record.disabled = p === 'starting' || p === 'stopping' || noDevices;
  mt.record.title = noDevices ? 'Подключи микрофон или наушники, список обновится сам' : '';
  mt.recDot.classList.toggle('stop', live);
  mt.recLabel.textContent = noDevices ? 'Нет устройств' : {
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
  stPaintLock();  // распознавание на время записи не меняется, и в настройках это видно
  paintModel();   // полоса загрузки модели на время записи из окна записи уходит
  paintBoosts();  // кнопка ускорения на время записи закрыта, а в окне записи её нет
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
  // booted: пока окно не решило, показывать выбор «с чего начать» или запись, микрофон
  // не открывается, иначе первый же запуск зажёг бы индикатор раньше, чем человек выбрал
  return booted && screen === 'meeting' && phase === 'idle' && document.visibilityState === 'visible'
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
    // Проверка микрофона из тренажёра может ещё закрываться (мы только что ушли
    // оттуда): monitor_start раньше её стопа получил бы «Идёт проверка микрофона»
    await micCheckQueue;
    const want = monitorWanted();
    if (want && (!monitorOn || restart)) {
      const r = await api().monitor_start(
        devIndex(mt.mic), devIndex(mt.spk), deviceName(mt.mic), deviceName(mt.spk),
      );
      setMonitorOn(!(r && r.error));
      if (r && r.error) paintHeard(r.error);
    } else if (!want && monitorOn) {
      setMonitorOn(false);
      await api().monitor_stop();
      lastLevels = [0, 0];
    }
  }).catch(() => { setMonitorOn(false); });
  return monitorQueue;
}

/**
 * Саундчек открыт или закрыт. Строка про индикатор видна, только пока он правда слушает.
 * Место под ней занято всегда: саундчек на долю секунды закрывается при каждом
 * перечитывании устройств, и строка, уходящая из потока, дёргала бы всё, что ниже.
 */
function setMonitorOn(on) {
  monitorOn = on;
  $('monitorNote').dataset.on = String(on);
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
    // Пустой список не повод закрывать кнопку: по нажатию он перечитывается,
    // и подключённое устройство появляется. Закрыта она только на время записи
    btn.disabled = phase === 'live' || phase === 'starting' || phase === 'stopping';
  }
}

/** Строки меню по списку устройств, который сейчас в select. */
function fillDeviceMenu(select, btn, menu, which) {
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
      if (opt.title) remember(which === 'me' ? 'micName' : 'spkName', opt.title);
      paintDeviceButtons();
      resetHeard(which);
      syncMonitor(true);
    });
    menu.appendChild(item);
  }
}

function openDeviceMenu(select, btn, menu, which) {
  const open = menu.hidden;
  closeMenu();
  if (!open) return;
  fillDeviceMenu(select, btn, menu, which);
  menu.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  (menu.querySelector('[aria-selected="true"]') || menu.querySelector('button'))?.focus();
  // Наушники, подключённые после запуска, появляются в открытом меню
  refreshDevices().then((changed) => {
    if (!changed || menu.hidden) return;
    fillDeviceMenu(select, btn, menu, which);
    (menu.querySelector('[aria-selected="true"]') || menu.querySelector('button'))?.focus();
  });
}

mt.micBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  openDeviceMenu(mt.mic, mt.micBtn, mt.micMenu, 'me');
});
mt.spkBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  openDeviceMenu(mt.spk, mt.spkBtn, mt.spkMenu, 'them');
});

// --- список устройств: читается при старте, при открытии меню и при возврате в окно ---

let devSig = '';          // отпечаток последнего списка: перерисовываем, только если он изменился
let devRefresh = null;    // идущее обновление, второе не запускаем
let devRefreshedAt = 0;

let devErrorShown = { text: '', at: 0 };

/** Ошибка чтения списка устройств человеку. Одну и ту же не чаще раза в полминуты. */
function devError(text) {
  const now = Date.now();
  if (text === devErrorShown.text && now - devErrorShown.at < 30000) return;
  devErrorShown = { text, at: now };
  toast(text, 6000);
}

/** Подпись состояния до записи: есть ли чем записывать. Не трогает занятые фазы. */
function paintIdleState() {
  if (phase !== 'idle') return;
  const hasMic = hasDevices(mt.mic);
  const hasSpk = hasDevices(mt.spk);
  if (!hasMic && !hasSpk) {
    paintState('Нет устройств для записи', 'error', 'Подключи микрофон или наушники, список обновится сам');
  } else if (!hasSpk) {
    paintState('Не нашёл звук собеседника', 'error', 'Проверь, что наушники подключены');
  } else if (!hasMic) {
    paintState('Не нашёл микрофон', 'error', 'Подключи его, а звук собеседника пишется и так');
  } else if (modelInfo.state === 'error') {
    // Строка под заголовком (ошибка и «Попробовать снова») уже есть, «Готово» рядом с ней врало бы
    paintState('Распознавание не готово', 'error', '');
  } else {
    paintState('Готово к записи', 'idle', 'Пока не начнёшь запись, звук никуда не пишется');
  }
}

/**
 * Кладёт свежий список устройств в списки записи. Выбор человека остаётся, если
 * его устройство на месте; иначе берётся запомненное, иначе устройство Windows
 * по умолчанию. Возвращает true, если список изменился.
 */
function applyDevices(d) {
  const sig = JSON.stringify([d.mics, d.loopbacks]);
  if (sig === devSig) return false;
  devSig = sig;
  const micName = deviceName(mt.mic) || remembered.micName || '';
  const spkName = deviceName(mt.spk) || remembered.spkName || '';
  fillSelect(mt.mic, d.mics, d.default_mic, micName);
  fillSelect(mt.spk, d.loopbacks, d.default_loopback, spkName);
  setPhase(phase);  // кнопка записи, подписи устройств и саундчек под новый список
  paintHeard();
  paintIdleState();
  return true;
}

/**
 * Перечитывает устройства и обновляет списки. Идёт в очереди саундчека: питон
 * на время чтения закрывает его (иначе наушники, подключённые позже, не
 * появятся), и открывать его заново надо уже по новому списку.
 */
function refreshDevices() {
  if (!window.pywebview) return Promise.resolve(false);
  if (phase !== 'idle' && phase !== 'done') return Promise.resolve(false);
  if (devRefresh) return devRefresh;
  devRefresh = monitorQueue = monitorQueue.then(async () => {
    const d = await api().get_devices();
    devRefreshedAt = Date.now();
    if (d.monitor_closed) { setMonitorOn(false); lastLevels = [0, 0]; }
    let changed = false;
    if (d.error) devError(d.error);
    else changed = applyDevices(d);
    syncMonitor();  // не ждём: мы внутри очереди саундчека
    return changed;
  }).catch(() => false).finally(() => { devRefresh = null; });
  return devRefresh;
}

// Вернулись в окно: наушники могли подключить, пока его не было. Саундчек на
// время чтения закрывается, поэтому слишком частые возвраты пропускаем
function comeBack() {
  if (!window.pywebview || Date.now() - devRefreshedAt < 3000) return;
  if (screen === 'meeting') refreshDevices();
  else if (screen === 'interview' && !iv.setup.hidden) ivRefreshMics();
}

window.addEventListener('focus', comeBack);
// Окно могли свернуть: фокус при этом не всегда приходит, видимость приходит
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') comeBack();
});

// Пока нет ни одного устройства, ищем их сами: подсказка «список обновится сам»
// должна быть правдой. Саундчек при этом не открыт, так что читать список дёшево
setInterval(() => {
  if (!window.pywebview || document.visibilityState !== 'visible') return;
  if (screen === 'meeting' && !hasDevices(mt.mic) && !hasDevices(mt.spk)) refreshDevices();
  else if (screen === 'interview' && !iv.setup.hidden && !hasDevices(iv.mic)) ivRefreshMics();
}, 4000);

function paintState(text, kind, sub) {
  mt.state.dataset.kind = kind || 'idle';
  mt.stateText.textContent = text;
  mt.stateSub.textContent = sub || '';
  // Ошибка переносится на несколько строк (см. style.css), а если и там не влезла,
  // целиком её видно по наведению
  mt.stateSub.title = kind === 'error' ? (sub || '') : '';
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
  // Ни микрофона, ни наушников: запись не начнётся (кнопка при этом неактивна)
  if (!hasDevices(mt.mic) && !hasDevices(mt.spk)) {
    toast('Нет ни микрофона, ни наушников. Подключи их, список обновится сам');
    return;
  }
  setPhase('starting');
  paintState('Запускаю', 'loading', $('asrOpen').textContent);
  // Саундчек должен закрыться раньше, чем откроется запись
  await monitorQueue;
  // Имена шлём вместе с номерами: номера устройств в Windows плавают,
  // и к моменту записи выбранный номер может указывать на другой вход.
  // Нет устройства: -1, питон тогда пишет только то, что есть
  let res;
  try {
    res = await api().start(
      devIndex(mt.mic), devIndex(mt.spk), deviceName(mt.mic), deviceName(mt.spk),
    );
  } catch {
    res = { error: 'Программа не отвечает, запись не началась' };
  }
  if (res && res.error) ui.error(res.error);
});

// Пробел начинает и останавливает запись, пока ничего не печатаешь
let spaceTaken = false;  // пробел уже стал записью: его отпускание не должно нажать кнопку
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || screen !== 'meeting' || typing() || $('dlg').open || openMenu()) return;
  const el = document.activeElement;
  const onButton = el && el !== mt.record && ['BUTTON', 'SUMMARY'].includes(el.tagName);
  // Пока идёт запись, пробел её останавливает, на какой бы кнопке ни стоял фокус: на
  // созвоне некогда целиться, а нажатая вместо этого кнопка путей или устройства
  // оставляла запись идти. Вне записи кнопки сохраняют свой пробел, иначе с клавиатуры
  // до них не добраться
  if (onButton && phase !== 'live') return;
  e.preventDefault();
  spaceTaken = true;
  if (!e.repeat) mt.record.click();
});
document.addEventListener('keyup', (e) => {
  if (e.code === 'Space' && spaceTaken) { e.preventDefault(); spaceTaken = false; }
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
    setMonitorOn(false);  // питон закрыл саундчек сам, перед тем как открыть запись
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

  /** removed: запись оказалась пустой, файл удалён, делиться путём не с чем. */
  stopped(report, removed) {
    recording = false;
    if (removed) currentPath = '';
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
    if (phase === 'stopping') {
      // Остановка сорвалась: записи уже нет, и красный огонёк с часами врать не
      // должны. В «done» не переходим: питон дошлёт stopped, когда закончит, а до
      // тех пор следующая запись встала бы за недописанной остановкой
      recording = false;
      document.body.classList.remove('recording');
      paintNavLive();
      paintState('Запись остановилась с ошибкой', 'error', message);
      return;
    }
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
    // Банк, который окно само только что сохранило из ответа нейросети, уже выбран и
    // объявлен тостом: второй раз о нём не говорим
    if (fresh && fresh === ivJustSaved) { ivJustSaved = ''; ivRenderBanks(); return; }
    // Свежий банк логично сразу и выбрать: за ним и шли
    if (fresh && !iv.setup.hidden) ivSelectBank(fresh);
    else ivRenderBanks();
    if (fresh) {
      const b = ivBanks.find((x) => x.file === fresh);
      toast(b ? `Новый банк: ${b.name}` : 'Список банков обновлён');
    }
  },

  /** Ход загрузки модели распознавания (см. «подготовка модели» выше). */
  modelState(info) {
    const was = modelInfo.state;
    applyModel(info);
    // Модель скачалась или сорвалась: галочка у «Распознавания» догоняет
    if (was !== info.state && info.state !== 'loading') refreshAsrReady();
  },

  /** Ход установки библиотек CUDA для видеокарты. */
  gpuState(info) {
    applyGpu(info);
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
  // Часы идут, пока записываешь. С нажатия «Остановить» запись кончилась, а хвост
  // ещё дописывается: часы стоят на том, сколько записано
  if (recording && phase === 'live') {
    const passed = (Date.now() - liveStart) / 1000;
    mt.clock.textContent = clockText(passed);
    $('navLiveText').textContent = clockText(passed);
  }
  if (micChecking) {
    checkSamples.push(lastLevels[0]);
    drawLive($('waveCheck'), checkSamples);
  }
  if (ivAnswering) ivTakeTick();
  // Установка CUDA идёт минуты и подолгу без новых строк: ход показывает время, раз в секунду
  if (gpuInfo.state === 'installing') {
    const sec = Math.floor(gpuElapsed());
    if (sec !== gpuShownSec) { gpuShownSec = sec; paintBoosts(); }
  }
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
  text: $('ivText'), followup: $('ivFollowup'), note: $('ivNote'),
  retry: $('ivRetry'), next: $('ivNext'), finish2: $('ivFinish2'),
};

// Что считать «поведенческим». Остальные темы идут как технические:
// делить приходится здесь, потому что в банке у вопроса только имя темы
const SOFT_THEMES = ['поведенческий', 'мотивация', 'о себе', 'софт'];

const IV_IDLE_HINT = 'Нажми красную кнопку или пробел и говори. Свой текст ты не увидишь, как на настоящем собесе.';

// Первый заход в тренажёр: пробный собес на три вопроса, строгость «разминка». Дальше
// банк запоминается (remember), и выбор человека сильнее этого
const IV_DEFAULT_BANK = 'Пробный собес.yaml';
// «Рабочий», а не «разминка»: на разминке программа не переспрашивает, а это главное,
// что стоит увидеть на первом мок-собесе. В пробном собесе все вопросы и так простые
const IV_DEFAULT_LEVEL = 'рабочий';
let ivDefaultsApplied = false;

// Запрос для нейросети живёт в питоне (interview.bank_prompt), здесь его нет:
// окно просит его у питона и копирует. Текст запроса один
let ivJustSaved = '';    // файл банка, который окно только что сохранило из ответа нейросети
let ivFixText = '';      // просьба исправить ответ, пока ответ с ошибками

let ivBanks = [];        // банки со списком вопросов, чтобы считать без запросов
let ivSavedThemes = {};  // выбор тем, запомненный по каждому банку
let ivBankFile = '';     // последний выбранный банк, при старте окна берётся из настроек
let ivChosen = new Set();
let ivCustom = false;    // темы выбираются вручную, чипы открыты
let ivLevels = [];
let ivLevel = 'рабочий';
let ivTheses = [];       // тезисы текущего вопроса, чтобы подписать разбор
let ivAnswering = false;
let ivTranscribing = false;  // ответ записан, питон его расшифровывает
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

/** Значок «у банка есть замечания»: что программа поправила или пропустила при чтении файла. */
function bankFlag(problems) {
  const flag = document.createElement('span');
  flag.className = 'flag';
  flag.innerHTML = icon('alert');
  const shown = problems.slice(0, 8);
  const more = problems.length - shown.length;
  flag.title = 'Замечания к банку:\n' + shown.map((p) => `- ${p}`).join('\n') + (more > 0 ? `\nи ещё ${more}` : '');
  flag.setAttribute('role', 'img');
  flag.setAttribute('aria-label', `У банка есть замечания: ${problems.length}`);
  return flag;
}

function ivRenderBanks() {
  iv.banks.innerHTML = '';
  if (!ivCurrentBank() && ivBanks.length) {
    // Первый заход или запомненный банк удалили: пробный собес, а если его нет, первый в списке
    const trial = ivBanks.find((b) => b.file === IV_DEFAULT_BANK && b.count);
    ivBankFile = (trial || ivBanks[0]).file;
  }
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
    const meta = row.children[2];
    if (b.problems && b.problems.length) meta.appendChild(bankFlag(b.problems));
    const label = document.createElement('span');
    label.textContent = b.count ? count(b.count, 'вопрос', 'вопроса', 'вопросов') : 'файл не читается';
    meta.appendChild(label);
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
  remember('ivBank', file);
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
    // «Поведенческие» или «Технические» в банке, где таких тем нет, выбрали бы пустой
    // набор, и кнопка молча вернулась бы на «Все»: лучше сразу закрыть её и сказать почему
    const empty = (b.dataset.preset === 'soft' || b.dataset.preset === 'tech')
      && (!bank || !ivSet(b.dataset.preset).size);
    b.disabled = empty;
    b.title = empty ? 'В этом банке таких тем нет' : '';
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
    if (b.disabled) return;
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
  // Без микрофона отвечать нечем: кнопка неактивна, причина написана рядом с ней.
  // Пока список устройств не прочитан, про «нет микрофона» молчим
  const noMic = !hasDevices(iv.mic);
  iv.start.title = noMic && ivMicsLoaded ? 'Подключи микрофон' : '';
  if (noMic && ivMicsLoaded) {
    $('ivSumCount').textContent = 'Нет микрофона';
    $('ivSumMeta').textContent = 'Подключи его, список обновится сам';
    iv.start.disabled = true;
    return;
  }
  $('ivSumCount').textContent = n ? count(n, 'вопрос', 'вопроса', 'вопросов') : 'Вопросов не осталось';
  $('ivSumMeta').textContent = n
    ? `Строгость «${level ? level.name : ''}», ${ivEstimate(picked)}`
    : 'Сними лишние темы или выбери строгость помягче';
  iv.start.disabled = n === 0 || noMic;
}

let ivMicsLoaded = false;  // список микрофонов хоть раз прочитан
let ivMicsSig = '';
let ivMicsBusy = false;

/**
 * Перечитывает микрофоны для тренировки. Выбор остаётся, если устройство на
 * месте: иначе тот, что выбран для записи встречи, запомненный, по умолчанию.
 * Пока идёт проверка микрофона, не трогаем: она держит устройство, и список
 * от этого не изменился бы.
 */
async function ivRefreshMics() {
  if (!window.pywebview || ivMicsBusy || micChecking) return false;
  ivMicsBusy = true;
  try {
    const d = await api().get_devices();
    devRefreshedAt = Date.now();
    // Чтение списка закрывает саундчек в питоне (см. _do_devices). Если окно успело
    // открыть его заново (быстро Ctrl+2 и обратно Ctrl+1, пока шла загрузка), здесь он
    // числится открытым, а в питоне закрыт, и дорожки стоят
    if (d.monitor_closed) {
      setMonitorOn(false);
      lastLevels = [0, 0];
      syncMonitor();
    }
    // Ушли из тренажёра, пока читался список: этот экран ответа больше не ждёт
    if (screen !== 'interview') return false;
    if (d.error) { devError(d.error); if (ivMicsLoaded) return false; }
    const sig = JSON.stringify(d.mics);
    ivMicsLoaded = true;
    if (sig === ivMicsSig) { ivUpdateStart(); return false; }
    ivMicsSig = sig;
    const wanted = deviceName(iv.mic) || deviceName(mt.mic) || remembered.micName || '';
    fillSelect(iv.mic, d.mics, d.default_mic, wanted);
    $('ivCheckMic').disabled = !hasDevices(iv.mic);
    ivUpdateStart();
    return true;
  } catch {
    return false;
  } finally {
    ivMicsBusy = false;
  }
}

async function ivLoadSetup() {
  const s = await api().interview_setup();
  ivLevels = s.levels || [];
  ivBanks = s.banks || [];
  ivSavedThemes = s.saved_categories || {};
  if (!ivDefaultsApplied) {
    ivDefaultsApplied = true;
    // Первый заход, банк ещё ни разу не выбирали: пробный собес и строгость «рабочий». Выбранный
    // банк запоминается в настройки.json (ivSelectBank), и тогда эти умолчания молчат
    if (!remembered.ivBank) {
      if (ivBanks.some((b) => b.file === IV_DEFAULT_BANK && b.count)) ivBankFile = IV_DEFAULT_BANK;
      if (ivLevels.some((l) => l.id === IV_DEFAULT_LEVEL)) ivLevel = IV_DEFAULT_LEVEL;
    }
  }
  if (!ivLevels.some((l) => l.id === ivLevel)) ivLevel = ivLevels.length ? ivLevels[0].id : 'рабочий';
  ivRenderLevels();
  ivRestoreThemes();

  await ivRefreshMics();
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

// --- свой банк под вакансию: запрос, ответ нейросети, проверка ---

$('ivCopyBankPrompt').addEventListener('click', async () => {
  let r;
  try {
    r = await api().bank_prompt($('ivVacancy').value);
  } catch {
    r = { error: 'Программа не отвечает, запрос не получен' };
  }
  if (r.error) { toast(r.error, 5000); return; }
  copyAndSay(r.text, 'Запрос скопирован. Вставь его в чат с нейросетью');
});

/** Итог проверки под кнопкой: заголовок, список строк и, если банк не принят, просьба исправить. */
function ivBankResult(kind, head, lines, fix) {
  const box = $('ivBankResult');
  box.hidden = !head;
  box.dataset.kind = kind;
  $('ivBankResultHead').textContent = head || '';
  const list = $('ivBankResultList');
  list.innerHTML = '';
  for (const text of lines || []) {
    const li = document.createElement('li');
    li.textContent = text;
    list.appendChild(li);
  }
  ivFixText = fix || '';
  $('ivCopyFix').hidden = !ivFixText;
  // На низком окне итог оказывается под полосой «Начать тренировку»: подводим его в видимую область
  if (head) box.scrollIntoView({ block: 'nearest' });
}

$('ivSaveBank').addEventListener('click', async () => {
  const btn = $('ivSaveBank');
  btn.disabled = true;
  let r;
  try {
    r = await api().bank_save($('ivReply').value);
  } catch {
    r = { ok: false, errors: ['Программа не отвечает, банк не сохранён'] };
  } finally {
    btn.disabled = false;
  }
  if (!r.ok) {
    ivBankResult('bad', 'Банк не сохранён. Вот что не так:', r.errors, r.fix);
    return;
  }
  // Банк уже в папке. Список приходит в этом же ответе, выбираем сохранённый и говорим об этом
  // один раз: слежение за папкой о нём промолчит (ivJustSaved)
  ivJustSaved = r.file;
  setTimeout(() => { if (ivJustSaved === r.file) ivJustSaved = ''; }, 8000);
  ivBanks = r.banks || ivBanks;
  ivSelectBank(r.file);
  $('ivReply').value = '';
  const notes = r.notes || [];
  ivBankResult('good', notes.length ? 'Банк сохранён и выбран. Что программа поправила:' : '', notes, '');
  toast(`Банк сохранён: ${r.name}, ${count(r.count, 'вопрос', 'вопроса', 'вопросов')}`, 4000);
});

$('ivCopyFix').addEventListener('click', () => {
  if (ivFixText) copyAndSay(ivFixText, 'Просьба скопирована. Вставь её в тот же чат, а новый ответ сюда');
});

$('ivBanksDir').addEventListener('click', () => api().open_banks_dir());

// --- проверка микрофона до начала ---

const MIC_CHECK_HELP = 'Нажми «Проверить» и скажи пару слов. Дорожка должна ожить.';

// Все действия с проверкой идут по очереди, как у саундчека: питон должен получать
// их в том порядке, в каком их хотел человек. Иначе уход из раздела мог бы обогнать
// ещё не вернувшийся старт, и микрофон остался бы открытым
let micCheckQueue = Promise.resolve();
const micCheckRun = (op) => (micCheckQueue = micCheckQueue.then(op).catch(() => {}));
let micCheckPending = false;  // нажатие обработано не до конца: повторное игнорируем

/** Закрывает проверку и показывает вердикт. quiet: просто закрыть, без вердикта. */
async function ivStopMicCheck(quiet) {
  const btn = $('ivCheckMic');
  const info = $('ivCheckText');
  micChecking = false;
  btn.textContent = 'Проверить';
  let r;
  try {
    r = await api().mic_check_stop();
  } catch {
    r = { error: 'Не удалось закрыть микрофон' };
  }
  if (quiet) {
    info.dataset.kind = '';
    info.title = '';
    info.textContent = MIC_CHECK_HELP;
    return;
  }
  if (r.error) { info.textContent = r.error; info.dataset.kind = 'bad'; return; }
  const ok = r.verdict === 'хорошо';
  info.dataset.kind = ok ? 'good' : 'bad';
  info.title = `пик ${r.peak}`;
  info.textContent = ok
    ? 'Слышу хорошо, можно начинать.'
    : 'Слишком тихо, ответы не запишутся. Выбери другой микрофон или подними усиление в настройках.';
}

async function ivStartMicCheck() {
  const btn = $('ivCheckMic');
  const info = $('ivCheckText');
  if (devIndex(iv.mic) < 0) {
    info.textContent = 'Микрофон не найден: подключи его, список обновится сам';
    info.dataset.kind = 'bad';
    return;
  }
  let r;
  try {
    r = await api().mic_check_start(devIndex(iv.mic), deviceName(iv.mic));
  } catch {
    r = { error: 'Программа не отвечает, микрофон не открылся' };
  }
  if (r.error) { info.textContent = r.error; info.dataset.kind = 'bad'; return; }
  micChecking = true;
  checkSamples = [];
  info.dataset.kind = '';
  btn.textContent = 'Готово';
  info.textContent = 'Говори обычным голосом…';
}

$('ivCheckMic').addEventListener('click', () => {
  // Повторное нажатие, пока ждём питон, не открывает второй захват
  if (micCheckPending) return;
  micCheckPending = true;
  const btn = $('ivCheckMic');
  btn.disabled = true;
  micCheckRun(async () => {
    try {
      if (micChecking) await ivStopMicCheck(false);
      else await ivStartMicCheck();
    } finally {
      micCheckPending = false;
      btn.disabled = !hasDevices(iv.mic);
    }
  });
});

/** Тихо закрывает проверку: ушли из раздела или начали тренировку. Ждать можно. */
function ivDropMicCheck() {
  return micCheckRun(async () => {
    if (micChecking) await ivStopMicCheck(true);
  });
}

// --- сессия ---

iv.start.addEventListener('click', async () => {
  if (ivBusy) return;
  if (devIndex(iv.mic) < 0) { toast('Не выбран микрофон: подключи его, список обновится сам', 5000); return; }
  ivBusy = true;
  iv.start.disabled = true;
  try {
    // Микрофон проверки должен освободиться до первого ответа
    await ivDropMicCheck();
    const r = await api().interview_start(
      ivBankFile, ivLevel, devIndex(iv.mic), deviceName(iv.mic), [...ivChosen], $('ivShuffle').checked,
    );
    if (r.error) { toast(r.error, 5000); return; }
    vt(() => ivRenderQuestion(r), 'question');
  } catch {
    toast('Программа не отвечает, тренировка не началась', 5000);
  } finally {
    ivBusy = false;
    ivUpdateStart();
  }
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
  // Подсказка из банка (поле «заметка»): свёрнутая строка, раскрывается по нажатию
  const note = (q.note || '').trim();
  iv.note.hidden = !note;
  iv.note.open = false;
  $('ivNoteText').textContent = note;
  ivPendingFollowup = '';
  ivNewTake();
  ivShow(iv.ask);
  ivDrawTake();
}

/** Кнопка ответа закрыта, пока модель распознавания скачивается или не скачалась. */
function ivPaintAnswerBtn() {
  if (ivAnswering || ivTranscribing) return;
  const wait = ivModelBlocks();
  const label = !wait ? 'Записать ответ'
    : modelInfo.state === 'error' ? 'Распознавание не готово' : 'Жду распознавание…';
  iv.answer.disabled = wait;
  iv.answer.setAttribute('aria-label', label);
  iv.answerLabel.textContent = label;
  iv.hint.textContent = !wait ? IV_IDLE_HINT
    : modelInfo.state === 'error' ? 'Распознавание не скачалось. Нажми «Попробовать снова» под вопросом.'
      : 'Распознавание ещё готовится. Кнопка оживёт сама, как только оно будет готово.';
}

function ivNewTake() {
  const max = ivTarget ? Math.max(ivTarget[1] * 1.4, ivTarget[1] + 20) : 90;
  ivTake = { samples: [], maxSec: Math.round(max), target: ivTarget };
  iv.timer.textContent = '0:00';
  iv.timer.classList.remove('over');
  ivPaintAnswerBtn();
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
  // Модель ещё скачивается (или не скачалась): ответ записывать некому. Кнопка в это время
  // закрыта, пробел сюда доходит, поэтому говорим словами
  if (!ivAnswering && ivModelBlocks()) {
    toast(modelInfo.state === 'error'
      ? 'Распознавание не готово. Нажми «Попробовать снова» под вопросом'
      : 'Распознавание ещё готовится, подожди', 4000);
    return;
  }
  ivBusy = true;
  iv.answer.disabled = true;

  if (!ivAnswering) {
    let r;
    try {
      r = ivPendingFollowup
        ? await api().followup_start(ivPendingFollowup)
        : await api().answer_start();
    } catch {
      r = { error: 'Программа не отвечает, запись ответа не началась' };
    }
    // Что бы ни вернулось, кнопка не остаётся неактивной
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
  ivTranscribing = true;
  document.body.classList.remove('answering');
  ivLastTake = { ...ivTake, samples: ivTake.samples.slice() };
  iv.answerLabel.textContent = 'Расшифровываю…';
  iv.hint.textContent = '';
  // «Пропустить» и «Завершить» остаются закрытыми, пока ответ расшифровывается: нажатые
  // раньше, они клали ответ на следующий вопрос или ломали сессию на последнем
  // Пока идёт расшифровка, по дорожке бежит блик: программа работает, а не зависла
  const timeline = iv.waveTake.parentElement;
  timeline.classList.add('busy');

  let r;
  try {
    r = await api().answer_stop();
  } catch {
    r = { error: 'Программа не отвечает, ответ не расшифрован' };
  }
  timeline.classList.remove('busy');
  ivBusy = false;
  ivTranscribing = false;
  iv.answer.disabled = false;
  iv.skip.disabled = iv.finish.disabled = false;
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
  iv.note.hidden = true;  // подсказка принадлежит основному вопросу, не уточнению
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
  // Два нажатия подряд: второй вызов получил бы от питона «сессия не начата»
  if (ivBusy) return;
  ivBusy = true;
  let r;
  try {
    r = await api().interview_finish();
  } catch {
    r = { error: 'Программа не отвечает, тренировка не сохранена' };
  }
  if (r.error) { ivBusy = false; toast(r.error, 5000); return; }
  ivPrompt = r.prompt || '';
  ivPath = r.path || '';
  const saved = !!r.path;
  const sum = r.summary || { rows: [], answered: 0, takes: 0, clean: 0, weak_ids: [] };
  ivWeakIds = sum.weak_ids || [];

  vt(() => {
    try {
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
    } finally {
      ivBusy = false;
    }
  }, 'question');
}

/**
 * Переход по тренировке: пропустить вопрос, следующий, ещё дубль. Пока вызов идёт
 * и пока экран не сменился, ivBusy занят: двойной Enter или двойной клик иначе
 * пропустили бы вопрос два раза.
 */
async function ivGo(call) {
  if (ivBusy) return;
  ivBusy = true;
  let r;
  try {
    r = await call();
  } catch {
    r = { error: 'Программа не отвечает, тренировка не продолжилась' };
  }
  if (r.error) { ivBusy = false; toast(r.error, 5000); return; }
  if (r.finished) { ivBusy = false; ivFinish(); return; }  // ivFinish займёт ivBusy сам
  // Занято остаётся до смены экрана: переход браузера идёт долю секунды, и кнопки
  // старого вопроса всё это время ещё нажимаются
  vt(() => {
    try { ivRenderQuestion(r); } finally { ivBusy = false; }
  }, 'question');
}

// Тот же банк и та же строгость, но только вопросы, где последний дубль не чистый
$('ivWeak').addEventListener('click', async () => {
  if (!ivWeakIds.length || ivBusy) return;
  ivBusy = true;
  let r;
  try {
    r = await api().interview_start(
      ivBankFile, ivLevel, devIndex(iv.mic), deviceName(iv.mic), [], false, ivWeakIds,
    );
  } catch {
    r = { error: 'Программа не отвечает, тренировка не началась' };
  } finally {
    ivBusy = false;
  }
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

// Имя, которое программа даёт файлу сама: «2026-10-01_16-04 расшифровка», а если в ту
// же минуту уже была запись, то «… расшифровка (2)». В списке оно читается как
// служебное, а дата и так стоит строкой ниже
const AR_DEFAULT_NAME = /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}( расшифровка)?( \(\d+\))?$/;
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

/** Заглушка справа, пока запись не выбрана. Пустой архив не должен звать выбрать запись слева. */
function arPaintEmpty() {
  const none = !arItems.length;
  const asked = ar.search.value.trim();
  $('arEmptyTitle').textContent = !none ? 'Выбери запись слева'
    : asked ? 'Ничего не нашлось' : 'Записей пока нет';
  $('arEmptyText').textContent = !none
    ? 'Здесь лежат расшифровки встреч и разборы тренировок. Поиск ищет по тексту внутри них.'
    : asked ? 'Попробуй другое слово. Поиск ищет по тексту реплик.'
      : 'Расшифровки встреч и разборы тренировок появятся здесь после первой записи.';
}

/** Кладёт текст в элемент и оборачивает найденное в <mark>: в списке видно, почему запись нашлась. */
function appendMarked(el, text, rx) {
  el.textContent = '';
  let last = 0;
  if (rx) {
    for (const hit of text.matchAll(rx)) {
      el.append(text.slice(last, hit.index));
      const mark = document.createElement('mark');
      mark.textContent = hit[0];
      el.append(mark);
      last = hit.index + hit[0].length;
    }
  }
  el.append(text.slice(last));
}

function arRender() {
  arPaintEmpty();
  const rx = arPattern(ar.search.value);
  const items = arKind === 'все' ? arItems : arItems.filter((i) => i.kind === arKind);
  ar.list.innerHTML = '';
  if (!items.length) {
    const p = document.createElement('p');
    p.className = 'empty-note';
    p.textContent = ar.search.value.trim()
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
      appendMarked(h, it.hits[0], rx);
      btn.appendChild(h);
    }
    btn.addEventListener('click', () => arOpen(it.path));
    ar.list.appendChild(btn);
  }
}

async function arLoad(query) {
  const asked = (query || '').trim();
  arItems = asked ? await api().archive_search(asked) : await api().archive_list();
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
  // Выжимка прошлой записи не должна ни висеть под новой, ни попадать под подсветку
  ar.digestBox.hidden = true;
  ar.digestBox.innerHTML = '';
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

/**
 * Как ищет archive.py: регистр не важен, «ё» и «е» одна буква (распознавание пишет
 * то так, то так), любая череда пробелов и переносов равна одному пробелу.
 */
function arPattern(query) {
  const words = (query || '').trim().split(/\s+/).filter(Boolean)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/[её]/gi, '[её]'));
  return words.length ? new RegExp(words.join('\\s+'), 'gi') : null;
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

  // Скрытое (выжимка, пока её не просили, пустой хвост) не подсвечиваем: совпадения
  // в нём считались бы, а увидеть их нельзя
  const walker = document.createTreeWalker(ar.view, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      const p = n.parentElement;
      return p && p.closest('.ar-body, .ar-tail, .ar-digest') && !p.closest('[hidden]')
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
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
  const path = arCurrent;
  const r = await api().archive_digest(path);
  if (r.error) { toast(r.error); return; }
  if (path !== arCurrent) return;  // пока считалось, открыли другую запись
  arShowDigest(r.digest);
  // Дописана ли она в файл на самом деле, решает питон: при повторном нажатии, битом
  // файле или отказе диска «дописана» было бы неправдой
  if (r.written) toast('Выжимка готова и дописана в файл');
  else toast(r.note || 'Выжимка готова', 5000);
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
  urlRow: $('stUrlRow'), url: $('stUrl'), urlNote: $('stUrlNote'), urlHint: $('stUrlHint'),
  apiModel: $('stApiModel'), apiModels: $('stApiModels'),
  folderRow: $('stFolderRow'), folder: $('stFolder'),
  key: $('stKey'), forget: $('stForget'), keysLink: $('stKeysLink'),
  lang: $('stLang'), glossary: $('stGlossaryNote'), test: $('stTest'), result: $('stResult'),
  lock: $('stLock'), patterns: $('stPatterns'), patternsRow: $('stPatternsRow'),
};

const ST_MODE_NOTE = {
  local: 'Звук никуда не уходит. С видеокартой NVIDIA успевает за речью.',
  api: 'Подойдёт любой компьютер. Нужен ключ сервиса.',
};

// Что прислал питон: текущий выбор, варианты, есть ли видеокарта и ключи
let stData = null;
let stMode = 'local';
// Модель запоминается в настройки.json, только если человек выбрал её сам: иначе в файле
// застряла бы та, что программа подставила, и после установки CUDA не сменилась бы на turbo
let stModelChosen = false;

const stProvider = () => stData.providers[st.provider.value];

/** Предупреждение про адрес своего сервера: http не на этом компьютере открыт всем в сети. */
function stUrlWarn(text) {
  st.urlNote.textContent = text || '';
  st.urlNote.hidden = !text;
}

/** Серая подсказка под полем адреса (или красная, kind 'bad'): про само поле, а не про всю форму. */
function stUrlHint(text, kind) {
  st.urlHint.textContent = text || '';
  st.urlHint.dataset.kind = kind || '';
  st.urlHint.hidden = !text;
}

// Пока идёт запись (или она запускается и останавливается), питон распознавание не
// меняет: модель уже в работе. Поля при этом закрыты, а не просто не сохраняются
const stLocked = () => phase === 'live' || phase === 'starting' || phase === 'stopping';

function stPaintLock() {
  const lock = stLocked();
  st.lock.hidden = !lock;
  const fields = [
    st.model, st.provider, st.url, st.apiModel, st.folder, st.key, st.forget, st.lang,
    ...st.mode.querySelectorAll('button'),
  ];
  for (const el of fields) el.disabled = lock;
}

function stResult(text, kind) {
  st.result.textContent = text || '';
  st.result.dataset.kind = kind || '';
  if (!text) stUrlHint('');  // «сохранено» и смена режима гасят и подсказку про адрес
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
  stModelChosen = !!stData.model_chosen;
  applyGpu(stData.gpu);
  stPaintHardware();
  st.now.textContent = `сейчас ${stData.label}`;
  stUrlWarn(stData.url_warning);
  // Личный глоссарий не прочитался: без него имена и термины распознаются хуже, и это
  // должно быть видно здесь, а не только в тихом сбое
  st.glossary.textContent = stData.glossary_note || '';
  st.glossary.hidden = !stData.glossary_note;
  stResult('');
  stPaint();
  stPaintLock();

  const s = await api().get_settings();
  paintGains(s);
  // Словари разбора речи, которые не прочитались целиком: одна строка, если они есть
  st.patterns.textContent = s.patterns_note || '';
  st.patternsRow.hidden = !s.patterns_note;
  applyModel(s.model);
}

/** Строка про железо под выбором модели. Видеокарта без библиотек CUDA, это отдельный случай. */
function stPaintHardware() {
  const g = gpuInfo;
  let text;
  let kind;
  if (stData.cuda) {
    text = 'Видеокарта NVIDIA найдена, большие модели успевают за речью.';
    kind = 'good';
  } else if (g.present && g.state === 'restart') {
    text = 'Библиотеки CUDA поставлены. После перезапуска распознавание пойдёт на видеокарте.';
    if (g.after_model) text += ` Сначала докачается модель, около ${g.after_model}.`;
    kind = 'good';
  } else if (g.present) {
    text = 'Видеокарта NVIDIA найдена, но библиотек CUDA нет, поэтому считает процессор. '
      + 'Пока они не стоят, для живой записи бери small или распознавание через интернет.';
    if (g.after_model) text += ` После ускорения докачается модель, около ${g.after_model}.`;
    kind = 'warn';
  } else {
    text = 'Видеокарты NVIDIA нет, считать будет процессор. Для живой записи бери small или распознавание через интернет.';
    kind = 'warn';
  }
  st.hardware.textContent = text;
  st.hardware.dataset.kind = kind;
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
let stDirty = false;  // поменяли, а сохранение ещё не прошло
let stTypingError = false;  // питон отказал, пока адрес набирали: красным он станет при уходе из поля

function stQueueSave(delay = 0) {
  stDirty = true;
  clearTimeout(stSaveTimer);
  stSaveTimer = setTimeout(() => stSaveNow(), delay);
}

// Адрес своего сервера готов к сохранению, когда в нём есть схема и имя сервера. Остальное
// (пробелы, логин, «?») проверяет питон. Недописанный адрес не ошибка, пока его набирают
const stUrlReady = (value) => /^https?:\/\/[^\s/?#@:]+/i.test(value.trim());
const stIsCustom = () => stMode === 'api' && st.provider.value === 'custom';

/** Короткая отметка «Сохранено» в шапке, чтобы было видно, что кнопка не нужна. */
function flashSaved() {
  const s = $('stSaved');
  s.hidden = false;
  replay(s);
  clearTimeout(stSavedTimer);
  stSavedTimer = setTimeout(() => { s.hidden = true; }, 1800);
}

/**
 * final: человек ушёл из поля адреса, с этого момента ошибка настоящая. Пока он набирает
 * (или адрес ещё пуст после выбора «Свой сервер»), недописанное не сохраняется и не
 * ругается красным: серая подсказка, что сохранится, когда адрес будет готов.
 */
async function stSaveNow(final = false) {
  stDirty = false;
  stTypingError = false;
  // Экран настроек ещё не загрузился: поля пустые, и сохранение записало бы их поверх
  // настоящих настроек. Загрузка сама заполнит поля тем, что сохранено
  if (!stData) return;
  // Запись началась в те доли секунды, что ждали конца набора: питон всё равно
  // откажет, так что говорим об этом сразу
  if (stLocked()) { stResult('Идёт запись, это не сохранилось. Остановись и поменяй ещё раз', 'bad'); return; }
  const custom = stIsCustom();
  const typed = st.url.value.trim();
  if (custom && !stUrlReady(typed) && !(final && typed)) {
    // Про адрес пишем под самим полем: строка «Проверить связь» стоит ниже языка и
    // на низком окне не видна рядом с пустым полем
    stResult('');
    if (final) stUrlHint('Не указан адрес сервера', 'bad');
    else stUrlHint(typed ? 'Адрес ещё не дописан, сохраню, когда он будет готов' : 'Впиши адрес сервера, и настройка сохранится');
    return;
  }
  stUrlHint('');
  const key = st.key.value;
  let res;
  try {
    res = await api().asr_save(stConfig(), key, stModelChosen);
  } catch {
    res = { error: 'Программа не отвечает, настройка не сохранилась' };
  }
  if (res.error) {
    // Пока в поле адреса печатают, строгая проверка питона только мешает: она станет красной,
    // когда человек уйдёт из поля или нажмёт «Проверить связь»
    const typing = custom && !final && document.activeElement === st.url;
    stTypingError = typing;
    stResult(res.error, typing ? '' : 'bad');
    return;
  }
  stData.keys = res.keys;
  stData.config = stConfig();
  stData.model_chosen = stModelChosen;
  if (key) st.key.value = '';
  st.now.textContent = `сейчас ${res.label}`;
  paintAsrLabel(res.label);
  paintAsrReady(res.asr);
  stUrlWarn(res.url_warning);
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

st.model.addEventListener('change', () => { stModelChosen = true; stPaint(); stQueueSave(); });
st.lang.addEventListener('change', () => stQueueSave());

st.provider.addEventListener('change', () => {
  // Модель одного сервиса другому не подходит: берём первую из его списка.
  // Ключ тоже чужой, отправлять его другому сервису нельзя
  st.apiModel.value = stProvider().models[0] || '';
  st.key.value = '';
  stUrlWarn('');
  stResult('');
  stPaint();
  stQueueSave();
});

// Текстовые поля сохраняются, когда перестал печатать
for (const field of [st.apiModel, st.url, st.folder]) {
  field.addEventListener('input', () => stQueueSave(800));
}

// Ушёл из поля адреса своего сервера: недописанное теперь ошибка, и сказать о ней пора
st.url.addEventListener('blur', () => {
  if (!stIsCustom()) return;
  clearTimeout(stSaveTimer);
  if (stDirty || stTypingError || !stUrlReady(st.url.value)) stSaveNow(true);
});

// Ключ сохраняется, когда ушёл из поля или нажал Enter: по буквам его слать незачем
st.key.addEventListener('change', () => { if (st.key.value.trim()) stQueueSave(); });
st.key.addEventListener('keydown', (e) => { if (e.key === 'Enter') st.key.blur(); });

st.keysLink.addEventListener('click', () => api().open_url(stProvider().keys_url));

st.forget.addEventListener('click', async () => {
  if (!stData) return; // поставщик и ключи известны только после загрузки настроек
  const sure = await ask({
    title: 'Забыть ключ?',
    text: `Ключ ${stProvider().title} удалится из диспетчера учётных данных Windows.`,
    ok: 'Забыть',
    danger: true,
  });
  if (!sure) return;
  let res;
  try {
    res = await api().asr_forget_key(st.provider.value);
  } catch {
    res = { error: 'Программа не отвечает, ключ не удалён' };
  }
  if (res.error) { toast(res.error, 6000); return; }
  stData.keys = res.keys;
  paintAsrReady(res.asr);
  stPaint();
  toast('Ключ удалён');
});

st.test.addEventListener('click', async () => {
  st.test.disabled = true;
  stResult('Проверяю…');
  let res;
  try {
    res = await api().asr_test(stConfig(), st.key.value);
  } catch {
    res = { error: 'Программа не отвечает, проверка не прошла' };
  }
  st.test.disabled = false;
  stUrlWarn(res.warning);
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

// Файл, брошенный в окно, иначе открылся бы как страница: окно ушло бы на
// file:// вместе с мостом pywebview, который ставится на любую страницу
for (const type of ['dragover', 'drop']) {
  window.addEventListener(type, (e) => e.preventDefault());
}

setPhase('idle');
redrawIdleTracks();
paintModeMark();
// Ширина вкладок меняется, когда догружается шрифт
document.fonts.ready.then(paintModeMark);
window.addEventListener('resize', paintModeMark);

window.addEventListener('pywebviewready', async () => {
  try {
    // Настройки первыми: запомненный выбор нужен, чтобы выбрать устройства и банк.
    // Если человек в прошлый раз выбрал не то, что Windows считает основным,
    // берётся его выбор (applyDevices)
    const s = await api().get_settings();
    remembered = s.remembered || {};
    if (!ivBankFile && remembered.ivBank) ivBankFile = remembered.ivBank;
    paintGains(s);
    paintAsrLabel(s.asr_label);
    paintAsrReady(s.asr);
    applyModel(s.model);
    applyGpu(s.gpu);
    // Первый запуск: сначала выбор «с чего начать». До него саундчек не открывается
    // (booted ещё ложь), так что индикатор микрофона не загорается сам по себе
    if (s.first_run) enterWelcome();
    const d = await api().get_devices();
    devRefreshedAt = Date.now();
    if (d.error) devError(d.error);
    applyDevices(d);
  } catch {
    // Без ответа питона нет ни устройств, ни настроек. Сказать об этом надо, а не
    // оставить окно с мёртвой кнопкой и без объяснений
    paintState('Программа не отвечает', 'error', 'Закрой окно и открой его заново');
  }
  document.body.classList.remove('boot');
  if (welcomeOn) $('chooseTrainer').focus();  // скрытое не фокусируется, поэтому после boot
  booted = true;
  setPhase('idle');
  paintHeard();
  redrawIdleTracks();
});

// Мост не поднялся: окно всё равно не должно остаться пустым, пока ждёт настройки
setTimeout(() => document.body.classList.remove('boot'), 5000);
