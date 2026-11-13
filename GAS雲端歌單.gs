const SPREADSHEET_ID = '1SS6N1WiBfFmPSLdY_3lYqyIeWl5tbAXtomFFZWpiK0g';
const SHEET_NAME = '歌曲資料庫';
const NOTES_SHEET_NAME = 'KTV-歌曲備註';
const PLAY_COUNTS_SHEET_NAME = 'KTV-歌曲排行';
const SONG_FIELDS = ['歌名', '主唱者', '語言', '歌曲代號', '歸屬'];
const SONG_HEADER = SONG_FIELDS.join('-');
const NOTES_HEADERS = ['歌曲檔名', '選項', 'Key', '自訂備註', '更新時間'];
const PLAY_COUNTS_HEADERS = ['歌曲檔名', '播放次數'];
const NAS_LIBRARY_SHEET_NAMES = ['KTV-NAS-快照-A', 'KTV-NAS-快照-B'];
const NAS_LIBRARY_HEADERS = ['項目代號', 'NAS 項目 JSON'];
const NAS_LIBRARY_MAX_PAGE_SIZE = 5000;
const NAS_LIBRARY_MAX_BATCH_SIZE = 2000;

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
    if (payload.action === 'nas_library_load') {
      return jsonResponse({ ok: true, data: readNasLibraryPage(payload) });
    }
    if (payload.action === 'nas_library_begin') {
      return jsonResponse({ ok: true, data: beginNasLibrarySnapshot(payload) });
    }
    if (payload.action === 'nas_library_append') {
      return jsonResponse({ ok: true, data: appendNasLibrarySnapshot(payload) });
    }
    if (payload.action === 'nas_library_commit') {
      return jsonResponse({ ok: true, data: commitNasLibrarySnapshot(payload) });
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

function nasLibraryProperties() {
  return PropertiesService.getScriptProperties();
}

function readNasLibraryPage(payload) {
  const shareKey = typeof payload.share_key === 'string' ? payload.share_key : '';
  const offset = Number(payload.offset);
  const requestedLimit = Number(payload.limit);
  if (!shareKey || shareKey.length > 512
      || !Number.isSafeInteger(offset) || offset < 0
      || !Number.isSafeInteger(requestedLimit) || requestedLimit < 1) {
    throw new Error('NAS 快照分頁參數無效。');
  }

  const properties = nasLibraryProperties();
  const activeShareKey = properties.getProperty('KTV_NAS_ACTIVE_SHARE_KEY');
  const snapshotId = properties.getProperty('KTV_NAS_ACTIVE_SNAPSHOT_ID');
  const sheetName = properties.getProperty('KTV_NAS_ACTIVE_SHEET');
  const total = Number(properties.getProperty('KTV_NAS_ACTIVE_TOTAL') || 0);
  if (activeShareKey !== shareKey || !snapshotId
      || !NAS_LIBRARY_SHEET_NAMES.includes(sheetName)) {
    return { found: false };
  }

  const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheet = spreadsheet.getSheetByName(sheetName);
  if (!sheet) return { found: false };
  const limit = Math.min(requestedLimit, NAS_LIBRARY_MAX_PAGE_SIZE);
  const count = Math.max(0, Math.min(limit, total - offset));
  const rows = count
    ? sheet.getRange(offset + 2, 1, count, NAS_LIBRARY_HEADERS.length).getValues()
    : [];
  let state = {};
  try {
    state = JSON.parse(properties.getProperty('KTV_NAS_ACTIVE_STATE') || '{}');
  } catch (error) {
    state = {};
  }
  return {
    found: true,
    snapshot_id: snapshotId,
    total,
    updated_at: Number(properties.getProperty('KTV_NAS_ACTIVE_UPDATED_AT') || 0),
    state,
    items: rows.map((row) => ({
      item_key: String(row[0] || ''),
      item: JSON.parse(String(row[1] || '{}')),
    })),
  };
}

function beginNasLibrarySnapshot(payload) {
  const shareKey = typeof payload.share_key === 'string' ? payload.share_key : '';
  const snapshotId = typeof payload.snapshot_id === 'string' ? payload.snapshot_id : '';
  if (!shareKey || shareKey.length > 512
      || !/^[a-f0-9]{32}$/.test(snapshotId)) {
    throw new Error('NAS 快照識別資料無效。');
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const properties = nasLibraryProperties();
    const activeSheet = properties.getProperty('KTV_NAS_ACTIVE_SHEET');
    const stagingSheet = activeSheet === NAS_LIBRARY_SHEET_NAMES[0]
      ? NAS_LIBRARY_SHEET_NAMES[1]
      : NAS_LIBRARY_SHEET_NAMES[0];
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = spreadsheet.getSheetByName(stagingSheet)
      || spreadsheet.insertSheet(stagingSheet);
    sheet.clearContents();
    sheet.getRange(1, 1, 1, NAS_LIBRARY_HEADERS.length)
      .setValues([NAS_LIBRARY_HEADERS]);
    properties.setProperties({
      KTV_NAS_STAGING_SHEET: stagingSheet,
      KTV_NAS_STAGING_SHARE_KEY: shareKey,
      KTV_NAS_STAGING_SNAPSHOT_ID: snapshotId,
    });
    return { snapshot_id: snapshotId };
  } finally {
    lock.releaseLock();
  }
}

function appendNasLibrarySnapshot(payload) {
  const items = payload.items;
  if (!Array.isArray(items) || items.length > NAS_LIBRARY_MAX_BATCH_SIZE) {
    throw new Error('NAS 快照批次大小無效。');
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const properties = nasLibraryProperties();
    const sheetName = properties.getProperty('KTV_NAS_STAGING_SHEET');
    if (properties.getProperty('KTV_NAS_STAGING_SHARE_KEY') !== payload.share_key
        || properties.getProperty('KTV_NAS_STAGING_SNAPSHOT_ID') !== payload.snapshot_id
        || !NAS_LIBRARY_SHEET_NAMES.includes(sheetName)) {
      throw new Error('NAS 快照批次已過期。');
    }

    const rows = items.map((record) => {
      const itemKey = typeof record.item_key === 'string' ? record.item_key : '';
      const source = record.item;
      if (!itemKey || itemKey.length > 512 || !source || typeof source !== 'object'
          || Array.isArray(source) || !isSafeSongFilename(String(source.filename || ''))) {
        throw new Error('NAS 快照項目格式無效。');
      }
      const item = {};
      ['filename', 'filesize', 'isfolder', 'resource_id', 'id', 'fid'].forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(source, key)) item[key] = source[key];
      });
      return [safeSheetText(itemKey), JSON.stringify(item)];
    });
    if (rows.length) {
      const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
      const sheet = spreadsheet.getSheetByName(sheetName);
      if (!sheet) throw new Error('找不到 NAS 快照工作表。');
      const startRow = sheet.getLastRow() + 1;
      const lastRow = startRow + rows.length - 1;
      if (lastRow > sheet.getMaxRows()) {
        sheet.insertRowsAfter(sheet.getMaxRows(), lastRow - sheet.getMaxRows());
      }
      sheet.getRange(startRow, 1, rows.length, NAS_LIBRARY_HEADERS.length).setValues(rows);
    }
    return { appended: rows.length };
  } finally {
    lock.releaseLock();
  }
}

function commitNasLibrarySnapshot(payload) {
  const shareKey = typeof payload.share_key === 'string' ? payload.share_key : '';
  const snapshotId = typeof payload.snapshot_id === 'string' ? payload.snapshot_id : '';
  const total = Number(payload.total);
  const sourceState = payload.state;
  if (!shareKey || shareKey.length > 512 || !/^[a-f0-9]{32}$/.test(snapshotId)
      || !Number.isSafeInteger(total) || total < 0
      || !sourceState || typeof sourceState !== 'object' || Array.isArray(sourceState)) {
    throw new Error('NAS 快照完成資料無效。');
  }

  const safeState = {};
  ['base_url', 'ssid', 'folder_fid', 'folder_name', 'link_path'].forEach((key) => {
    if (typeof sourceState[key] === 'string') safeState[key] = sourceState[key].slice(0, 2048);
  });
  if (typeof sourceState.snapshot_partial === 'boolean') {
    safeState.snapshot_partial = sourceState.snapshot_partial;
  }
  if (!safeState.folder_fid) throw new Error('NAS 快照缺少資料夾識別資料。');

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const properties = nasLibraryProperties();
    const sheetName = properties.getProperty('KTV_NAS_STAGING_SHEET');
    if (properties.getProperty('KTV_NAS_STAGING_SHARE_KEY') !== shareKey
        || properties.getProperty('KTV_NAS_STAGING_SNAPSHOT_ID') !== snapshotId
        || !NAS_LIBRARY_SHEET_NAMES.includes(sheetName)) {
      throw new Error('NAS 快照完成作業已過期。');
    }
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet || sheet.getLastRow() - 1 !== total) {
      throw new Error('NAS 快照筆數與寫入資料不一致。');
    }
    properties.setProperties({
      KTV_NAS_ACTIVE_SHEET: sheetName,
      KTV_NAS_ACTIVE_SHARE_KEY: shareKey,
      KTV_NAS_ACTIVE_SNAPSHOT_ID: snapshotId,
      KTV_NAS_ACTIVE_TOTAL: String(total),
      KTV_NAS_ACTIVE_STATE: JSON.stringify(safeState),
      KTV_NAS_ACTIVE_UPDATED_AT: String(Date.now() / 1000),
    });
    properties.deleteProperty('KTV_NAS_STAGING_SHEET');
    properties.deleteProperty('KTV_NAS_STAGING_SHARE_KEY');
    properties.deleteProperty('KTV_NAS_STAGING_SNAPSHOT_ID');
    return { snapshot_id: snapshotId };
  } finally {
    lock.releaseLock();
  }
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