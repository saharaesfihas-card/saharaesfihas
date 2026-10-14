'use strict';
// Render Static Site: publish only customer assets, never management files or private data.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = path.join(root, '.render-cardapio-public');
const assets = ['index.html', 'app.js', 'catalog-live.js', 'address.js', 'delivery-fees.js',
  'style.css', 'ordering.js', 'ordering.css', 'pwa.js', 'pwa.css', 'service-worker.js',
  'system-config.js', 'manifest.webmanifest', 'logo-sahara.jpg', 'esfihas.png'];
fs.rmSync(output, { recursive: true, force: true });
fs.mkdirSync(output, { recursive: true });
for (const asset of assets) fs.copyFileSync(path.join(root, asset), path.join(output, asset));
for (const folder of ['images', 'icons']) fs.cpSync(path.join(root, folder), path.join(output, folder), { recursive: true });
console.log(`Cardápio público preparado: ${assets.length} arquivos e imagens/ícones.`);
