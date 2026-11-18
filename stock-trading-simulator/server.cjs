const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, 'dist');
const port = Number(process.env.PORT || 5188);
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
http.createServer((req, res) => {
  let pathname;
  try { pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname); }
  catch { res.writeHead(400).end('Bad request'); return; }
  const file = path.resolve(root, '.' + (pathname === '/' ? '/index.html' : pathname));
  if (!file.startsWith(root + path.sep)) { res.writeHead(403).end('Forbidden'); return; }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404).end('Not found'); return; }
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
    res.end(data);
  });
}).on('error', error => {
  console.error(error.code === 'EADDRINUSE' ? `端口 ${port} 已占用。若模拟盘已经启动，请直接访问 http://127.0.0.1:${port}；否则设置 PORT 使用其他端口。` : error.message);
  process.exitCode = 1;
}).listen(port, '127.0.0.1', () => {
  const url = `http://127.0.0.1:${port}`;
  console.log(`模拟交易台已启动：${url}`);
  if (process.argv.includes('--open') && process.platform === 'win32') {
    require('node:child_process').execFile('cmd.exe', ['/c', 'start', '', url], { windowsHide: true }, error => {
      if (error) console.log('请在浏览器中手动打开上方地址。');
    });
  }
});
