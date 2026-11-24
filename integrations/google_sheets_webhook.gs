// Deploy as a Google Apps Script web app: execute as owner, access Anyone.
// Script properties: LOONS_SHEET_ID and LOONS_WEBHOOK_SECRET.
// Do not place the shared secret in this file or in a public spreadsheet.
function doPost(e) {
  let stage = 'CONFIGURATION';
  try {
    const settings = PropertiesService.getScriptProperties();
    const secret = settings.getProperty('LOONS_WEBHOOK_SECRET');
    const sheetId = settings.getProperty('LOONS_SHEET_ID');
    if (!secret || !sheetId || !e || !e.postData) throw new Error('configuration');
    stage = 'REQUEST';
    const request = JSON.parse(e.postData.contents);
    if (typeof request.payload !== 'string' || typeof request.signature !== 'string') throw new Error('request');
    stage = 'SIGNATURE';
    const digest = Utilities.computeHmacSha256Signature(request.payload, secret, Utilities.Charset.UTF_8)
      .map(b => ('0' + (b & 255).toString(16)).slice(-2)).join('');
    if (request.signature.length !== digest.length || !constantTimeEqual(request.signature, digest)) throw new Error('signature');
    stage = 'PAYLOAD';
    const data = JSON.parse(request.payload);
    if (!/^\d{1,16}$/.test(String(data.order_number)) || typeof data.answers !== 'string' || data.answers.length > 45000)
      throw new Error('payload');
    stage = 'LOCK';
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      stage = 'SHEET_OPEN';
      const sheet = SpreadsheetApp.openById(sheetId).getSheetByName('Ответы');
      stage = 'SHEET_MISSING';
      if (!sheet) throw new Error('sheet');
      stage = 'SHEET_WRITE';
      const last = sheet.getLastRow();
      const ids = last > 1 ? sheet.getRange(2, 1, last - 1, 1).getDisplayValues() : [];
      let index = ids.findIndex(row => row[0] === String(data.order_number));
      const target = index >= 0 ? index + 2 : last + 1;
      const values = [data.order_number, data.updated_at, data.name, data.email,
                      data.phone, data.answers, data.getcourse].map(safeCell);
      sheet.getRange(target, 1, 1, 7).setNumberFormat('@').setValues([values]);
      SpreadsheetApp.flush();
    } finally {
      lock.releaseLock();
    }
    return ContentService.createTextOutput(JSON.stringify({ok: true, order_number: String(data.order_number)}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (_) {
    // Never echo submitted contact details, answers, signatures or credentials.
    return ContentService.createTextOutput(JSON.stringify({ok: false, code: stage})).setMimeType(ContentService.MimeType.JSON);
  }
}

function constantTimeEqual(a, b) {
  let diff = 0;
  for (let i = 0; i < b.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function safeCell(value) {
  const text = String(value == null ? '' : value);
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}
