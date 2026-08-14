const http = require('http');
const fs = require('fs');
const path = require('path');
const { WebSocketServer } = require('ws');

const PORT = Number(process.env.PORT) || 8080;
const receiverPath = path.join(__dirname, 'receiver.html');

const server = http.createServer((req, res) => {
  if (req.url === '/' || req.url === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(fs.readFileSync(receiverPath));
  } else {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('not found');
  }
});

const wss = new WebSocketServer({ server });

let sender = null;
let receiver = null;

function send(ws, obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

function notifyPeerReady() {
  if (sender && receiver) send(sender, { type: 'peer-ready' });
}

wss.on('connection', (ws, req) => {
  let role = 'receiver';
  try {
    role = new URL(req.url, 'http://localhost').searchParams.get('role') || 'receiver';
  } catch (e) {}
  if (role === 'sender') sender = ws;
  else receiver = ws;
  notifyPeerReady();

  ws.on('message', (data) => {
    let msg;
    try { msg = JSON.parse(data.toString()); } catch (e) { return; }
    if (role === 'sender') send(receiver, msg);
    else send(sender, msg);
  });

  ws.on('close', () => {
    if (role === 'sender') {
      if (sender === ws) sender = null;
      send(receiver, { type: 'peer-gone' });
    } else {
      if (receiver === ws) receiver = null;
      send(sender, { type: 'peer-gone' });
    }
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log('workee-screenshare listening on http://0.0.0.0:' + PORT);
});
