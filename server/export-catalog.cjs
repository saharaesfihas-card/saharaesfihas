'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const end = source.indexOf(';const cart=new Map();');
if (end < 0) throw new Error('Não foi possível localizar o catálogo do cardápio.');
const result = vm.runInNewContext(source.slice(0, end) + ';JSON.stringify(catalog)', {}, { timeout: 1000 });
process.stdout.write(result);
