const SPREADSHEET_ID = '1SS6N1WiBfFmPSLdY_3lYqyIeWl5tbAXtomFFZWpiK0g';
const SHEET_NAME = '歌曲資料庫';
const NOTES_SHEET_NAME = 'KTV-歌曲備註';
const PLAY_COUNTS_SHEET_NAME = 'KTV-歌曲排行';
const SONG_FIELDS = ['歌名', '主唱者', '語言', '歌曲代號', '歸屬'];
const SONG_HEADER = SONG_FIELDS.join('-');
const NOTES_HEADERS = ['歌曲檔名', '選項', 'Key', '自訂備註', '更新時間'];
const PLAY_COUNTS_HEADERS = ['歌曲檔名', '播放次數'];

function doGet() {
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle('KTV 雲端歌單')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function doPost(e) {
  try {
    const payload = JSON.parse(e && e.postData && e.postData.contents || '{}');
    const configuredToken = PropertiesService.getScriptProperties()
      .getProperty('KTV_SYNC_API_TOKEN');
    if (!configuredToken || payload.token !== configuredToken) {
      return jsonResponse({ ok: false, error: '未授權的同步請求。' });
    }

    if (payload.action === 'load') {
      return jsonResponse({ ok: true, data: readKtvSnapshot() });
    }
    if (payload.action !== 'sync') {
      return jsonResponse({ ok: false, error: '不支援的操作。' });
    }

    const snapshot = validateKtvSnapshot(payload);
    const lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      writeKtvSnapshot(snapshot);
    } finally {
      lock.releaseLock();
    }
    return jsonResponse({ ok: true });
  } catch (error) {
    console.error(`KTV 資料同步失敗：${error.message}`);
    return jsonResponse({ ok: false, error: '資料同步失敗，請檢查 API 設定與資料格式。' });
  }
}

function jsonResponse(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function validateKtvSnapshot(payload) {
  const notes = payload.song_notes;
  const playCounts = payload.song_play_counts;
  if (!notes || typeof notes !== 'object' || Array.isArray(notes)
      || !playCounts || typeof playCounts !== 'object' || Array.isArray(playCounts)) {
    throw new Error('同步資料格式無效。');
  }

  const cleanNotes = {};
  Object.keys(notes).forEach((filename) => {
    if (!isSafeSongFilename(filename)) return;
    const note = notes[filename];
    if (!note || typeof note !== 'object' || Array.isArray(note)) return;
    cleanNotes[filename] = {
      selected_options: Array.isArray(note.selected_options)
        ? note.selected_options.filter((item) => typeof item === 'string').slice(0, 20)
        : [],
      key_value: typeof note.key_value === 'string' ? note.key_value.slice(0, 20) : '原 Key',
      custom_text: typeof note.custom_text === 'string' ? note.custom_text.slice(0, 500) : '',
      updated_at: typeof note.updated_at === 'string' ? note.updated_at.slice(0, 50) : '',
    };
  });

  const cleanPlayCounts = {};
  Object.keys(playCounts).forEach((filename) => {
    const count = playCounts[filename];
    if (isSafeSongFilename(filename)
        && Number.isSafeInteger(count) && count >= 0) {
      cleanPlayCounts[filename] = count;
    }
  });
  return { song_notes: cleanNotes, song_play_counts: cleanPlayCounts };
}

function isSafeSongFilename(filename) {
  return typeof filename === 'string'
    && filename.length > 0
    && filename.length <= 255
    && filename !== '.'
    && filename !== '..'
    && !/[\\/]/.test(filename);
}

function readKtvSnapshot() {
  const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
  return {
    song_notes: readNotesSheet(spreadsheet),
    song_play_counts: readPlayCountsSheet(spreadsheet),
  };
}

function readNotesSheet(spreadsheet) {
  const sheet = spreadsheet.getSheetByName(NOTES_SHEET_NAME);
  if (!sheet || sheet.getLastRow() < 2) return {};
  const rows = sheet.getRange(2, 1, sheet.getLastRow() - 1, NOTES_HEADERS.length).getValues();
  const notes = {};
  rows.forEach((row) => {
    const filename = String(row[0] || '');
    if (!isSafeSongFilename(filename)) return;
    let options = [];
    try {
      const parsed = JSON.parse(String(row[1] || '[]'));
      if (Array.isArray(parsed)) options = parsed.filter((item) => typeof item === 'string');
    } catch (error) {
      options = [];
    }
    notes[filename] = {
      selected_options: options,
      key_value: String(row[2] || '原 Key'),
      custom_text: String(row[3] || ''),
      updated_at: row[4] instanceof Date ? row[4].toISOString() : String(row[4] || ''),
    };
  });
  return notes;
}

function readPlayCountsSheet(spreadsheet) {
  const sheet = spreadsheet.getSheetByName(PLAY_COUNTS_SHEET_NAME);
  if (!sheet || sheet.getLastRow() < 2) return {};
  const rows = sheet.getRange(2, 1, sheet.getLastRow() - 1, PLAY_COUNTS_HEADERS.length).getValues();
  const counts = {};
  rows.forEach((row) => {
    const filename = String(row[0] || '');
    const count = Number(row[1]);
    if (isSafeSongFilename(filename) && Number.isSafeInteger(count) && count >= 0) {
      counts[filename] = count;
    }
  });
  return counts;
}

function writeKtvSnapshot(snapshot) {
  const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
  const notesRows = Object.keys(snapshot.song_notes).sort().map((filename) => {
    const note = snapshot.song_notes[filename];
    return [
      safeSheetText(filename),
      JSON.stringify(note.selected_options),
      safeSheetText(note.key_value),
      safeSheetText(note.custom_text),
      safeSheetText(note.updated_at),
    ];
  });
  const playCountRows = Object.keys(snapshot.song_play_counts).sort().map((filename) => [
    safeSheetText(filename),
    snapshot.song_play_counts[filename],
  ]);
  replaceKtvSheet(spreadsheet, NOTES_SHEET_NAME, NOTES_HEADERS, notesRows);
  replaceKtvSheet(spreadsheet, PLAY_COUNTS_SHEET_NAME, PLAY_COUNTS_HEADERS, playCountRows);
}

function replaceKtvSheet(spreadsheet, sheetName, headers, rows) {
  const sheet = spreadsheet.getSheetByName(sheetName) || spreadsheet.insertSheet(sheetName);
  sheet.clearContents();
  const values = [headers].concat(rows);
  sheet.getRange(1, 1, values.length, headers.length).setValues(values);
  sheet.setFrozenRows(1);
}

function safeSheetText(value) {
  const text = String(value == null ? '' : value);
  return /^[=+\-@]/.test(text) ? `'${text}` : text;
}

function getSongs() {
  const sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SHEET_NAME);
  if (!sheet) {
    throw new Error(`找不到「${SHEET_NAME}」分頁，請確認分頁名稱。`);
  }

  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (!lastRow || !lastColumn) return [];

  const values = sheet.getRange(1, 1, lastRow, lastColumn).getDisplayValues();
  let headerRowIndex = -1;
  let songColumnIndex = -1;

  for (let rowIndex = 0; rowIndex < Math.min(values.length, 20); rowIndex += 1) {
    const rowHeaders = values[rowIndex].map(normalizeHeader);
    songColumnIndex = rowHeaders.indexOf(normalizeHeader(SONG_HEADER));
    if (songColumnIndex !== -1) {
      headerRowIndex = rowIndex;
      break;
    }
  }

  if (songColumnIndex === -1) {
    throw new Error(`找不到標題「${SONG_HEADER}」，請確認欄位名稱及分頁內容。`);
  }

  return values.slice(headerRowIndex + 1)
    .map((row) => {
      const parts = String(row[songColumnIndex] == null ? '' : row[songColumnIndex])
        .split('-')
        .map((part) => part.trim());
      const song = {};
      SONG_FIELDS.forEach((field, index) => {
        song[field] = index === SONG_FIELDS.length - 1
          ? parts.slice(index).join('-')
          : parts[index] || '';
      });
      return song;
    })
    .filter((song) => SONG_FIELDS.some((field) => song[field]));
}

function normalizeHeader(value) {
  return String(value == null ? '' : value)
    .trim()
    .replace(/[\s_\-－]/g, '');
}