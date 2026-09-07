// Encrypted credential storage for workee.
//
// Backend selection (checked once at init):
//   1. Electron safeStorage, when the OS keyring is actually usable
//      (isEncryptionAvailable() true). Data format: { v: 2, enc: <base64> }.
//   2. Fallback: scrypt(NID) -> AES-256-GCM keyed to this machine + user.
//      Data format: { v: 3, salt, iv, tag, data } (all base64). The NID
//      binds the ciphertext to this host/user; it cannot be decrypted on
//      another machine. No plaintext ever touches disk either way.
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
let safeStorage = null;
try {
  // Optional: only present in the main process with full Electron APIs.
  const electron = require('electron');
  safeStorage = electron.safeStorage || null;
} catch (e) {}

let file = '';
let backend = 'none';
let nid = null;

function available() {
  if (backend === 'none') return false;
  try {
    fs.accessSync(file);
    return true;
  } catch (e) {
    return false;
  }
}

function init(dir) {
  file = path.join(dir, 'credentials.enc');
  if (safeStorage && typeof safeStorage.isEncryptionAvailable === 'function') {
    let ok = false;
    try {
      ok = !!safeStorage.isEncryptionAvailable();
    } catch (e) {}
    if (ok) {
      backend = 'safeStorage';
      return;
    }
  }
  // Fallback: machine+user bound key.
  const id = [os.hostname(), os.userInfo().username, 'workee-cred-v1'].join('|');
  nid = crypto.createHash('sha256').update(id).digest();
  backend = 'scrypt-aesgcm';
}

function deriveKey(salt) {
  return crypto.scryptSync(nid, salt, 32, { N: 16384, r: 8, p: 1 });
}

function save(plaintext) {
  if (!plaintext || backend === 'none') return false;
  let payload;
  if (backend === 'safeStorage') {
    const enc = safeStorage.encryptString(plaintext);
    payload = { v: 2, enc: enc.toString('base64') };
  } else {
    const salt = crypto.randomBytes(16);
    const iv = crypto.randomBytes(12);
    const key = deriveKey(salt);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
    const data = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
    const tag = cipher.getAuthTag();
    payload = {
      v: 3,
      salt: salt.toString('base64'),
      iv: iv.toString('base64'),
      tag: tag.toString('base64'),
      data: data.toString('base64'),
    };
  }
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(payload), { mode: 0o600 });
    fs.renameSync(tmp, file);
    try { fs.chmodSync(file, 0o600); } catch (e) {}
    return true;
  } catch (e) {
    console.error('[credentials] save failed: ' + e.message);
    return false;
  }
}

function load() {
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (raw.v === 2) {
      const buf = Buffer.from(raw.enc, 'base64');
      const dec = safeStorage.decryptString(buf);
      return String(dec);
    }
    if (raw.v === 3) {
      const salt = Buffer.from(raw.salt, 'base64');
      const iv = Buffer.from(raw.iv, 'base64');
      const tag = Buffer.from(raw.tag, 'base64');
      const data = Buffer.from(raw.data, 'base64');
      const key = deriveKey(salt);
      const decipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
      decipher.setAuthTag(tag);
      const plain = Buffer.concat([decipher.update(data), decipher.final()]);
      return plain.toString('utf8');
    }
    return null;
  } catch (e) {
    console.error('[credentials] load failed (' + backend + '): ' + e.message);
    return null;
  }
}

function clear() {
  try { fs.unlinkSync(file); } catch (e) {}
}

module.exports = { init, available, save, load, clear, getBackend: function () { return backend; } };
