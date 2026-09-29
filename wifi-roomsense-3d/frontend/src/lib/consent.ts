/**
 * The recording consent statement shown to the operator.
 *
 * This MUST be the exact text of CONSENT_STATEMENT_V1 in
 * backend/roomsense/storage/models.py: the backend stores the version with
 * each consent record, and a record may only claim agreement to the text
 * that was actually shown. If the backend bumps the version, update both.
 */

export const CONSENT_STATEMENT_VERSION = 'consent-v1';

export const CONSENT_STATEMENT_TEXT =
  'RoomSense recording consent (consent-v1)\n' +
  '\n' +
  '1. Recording is opt-in. Nothing is recorded unless the operator starts a ' +
  'recording and confirms this statement.\n' +
  '2. Recording happens only in a space that the operator owns or controls.\n' +
  '3. Every person present in the sensed area has been told what is being ' +
  "recorded (Wi-Fi channel state information from the operator's own ESP32 " +
  'boards) and why, and has agreed to it.\n' +
  '4. Recording is local: recordings are stored only on this computer. ' +
  'Nothing is uploaded or shared by the application.\n' +
  '5. Any recording can be deleted at any time from the Recordings page.\n' +
  '6. No identification is performed. RoomSense does not recognise, name or ' +
  'count people, and no names are collected with this consent record.\n';

export const CONSENT_CHECKBOX_TEXT = 'All people present have been informed and agreed';
