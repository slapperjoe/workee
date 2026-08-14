const fs = require('fs');
const path = require('path');

let file = '';
let data = {};

function init(dir) {
  file = path.join(dir, 'settings.json');
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    data = {};
  }
}

function get(key, fallback) {
  return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback;
}

function set(key, value) {
  data[key] = value;
  try {
    fs.writeFileSync(file, JSON.stringify(data, null, 2));
  } catch (e) {}
}

module.exports = { init, get, set };
