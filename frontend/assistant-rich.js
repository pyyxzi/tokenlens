/* Small, DOM-independent renderer. Model prose is text, never executable HTML. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TLAssistantRich = api;
})(typeof window === 'object' ? window : null, function () {
  'use strict';
  const MAX_INPUT = 100000;
  const MAX_DEPTH = 5;
  const MAX_BLOCKS = 2000;
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const word = char => !!char && /[\p{L}\p{N}]/u.test(char);
  const punctuation = char => !!char && /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/.test(char);
  const space = char => !char || /\s/.test(char);

  function safeUrl(value) {
    if (typeof value !== 'string' || value.length > 2048 || !/^https?:\/\//i.test(value) || /[\s\u0000-\u001f\u007f\\]/.test(value)) return null;
    try {
      const url = new URL(value);
      return /^(https?:)$/.test(url.protocol) && url.hostname && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }

  function safeFilter(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const keys = Object.keys(value), allowed = new Set(['range', 'tool', 'model', 'project', 'session', 'cost_basis', 'q', 'project_paths', 'from', 'to']);
    if (!keys.length || keys.length > allowed.size || !validRange(value.range)) return null;
    const result = Object.create(null);
    for (const key of keys) {
      if (!allowed.has(key) || typeof value[key] !== 'string' || value[key].length > (key === 'range' ? 64 : 4096) || /[\u0000-\u001f\u007f]/.test(value[key])) return null;
      if (key === 'cost_basis' && !['total_cost','historical_cost','repriced_cost'].includes(value[key])) return null;
      if (['from','to'].includes(key) && value[key] && !validRange(value[key]+'~'+value[key])) return null;
      result[key] = value[key];
    }
    return result;
  }

  function validRange(value) {
    if (typeof value !== 'string') return false;
    if (['all', 'today', '1d', '7d', '30d', '90d', '180d', '365d', 'ytd'].includes(value)) return true;
    if (!/^\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    return value.split('~').every(date => {
      const parsed = new Date(date + 'T00:00:00Z');
      return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date;
    });
  }

  function evidenceMap(rows) {
    const result = new Map();
    if (!Array.isArray(rows)) return result;
    for (const row of rows.slice(0, 10000)) {
      if (!row || typeof row.id !== 'string' || !/^E\d{1,8}$/.test(row.id) || result.has(row.id)) continue;
      const filter = safeFilter(row.filter);
      if (filter) result.set(row.id, filter);
    }
    return result;
  }

  // Build matching delimiter positions once. A long line of unmatched brackets
  // or stars must not trigger a new scan of its remaining suffix each time.
  function delimiters(text) {
    const ticks = new Map(), codes = new Map(), brackets = new Map(), parentheses = new Map(), emphasis = new Map();
    for (let i = 0; i < text.length;) {
      if (text[i] === '\\' && punctuation(text[i + 1])) { i += 2; continue; }
      if (text[i] !== '`') { i++; continue; }
      let end = i + 1;
      while (text[end] === '`') end++;
      const size = end - i;
      if (!ticks.has(size)) ticks.set(size, []);
      ticks.get(size).push(i);
      i = end;
    }
    for (const [size, positions] of ticks) {
      for (let i = 0; i + 1 < positions.length; i += 2) codes.set(positions[i], {end:positions[i + 1], size});
    }
    const squareStack = [], roundStack = [], emphasisStack = new Map();
    for (let i = 0; i < text.length;) {
      if (text[i] === '\\' && punctuation(text[i + 1])) { i += 2; continue; }
      const code = codes.get(i);
      if (code) { i = code.end + code.size; continue; }
      const char = text[i];
      if (char === '[') squareStack.push(i);
      else if (char === ']' && squareStack.length) brackets.set(squareStack.pop(), i);
      else if (char === '(') roundStack.push(i);
      else if (char === ')' && roundStack.length) parentheses.set(roundStack.pop(), i);
      if (char === '*' || char === '_') {
        let end = i + 1;
        while (text[end] === char) end++;
        const size = end - i, key = char + size;
        if (size <= 3 && !(char === '_' && word(text[i - 1]) && word(text[end]))) {
          const stack = emphasisStack.get(key) || [];
          if (!space(text[i - 1]) && stack.length) emphasis.set(stack.pop(), {end:i, size});
          else if (!space(text[end])) stack.push(i);
          emphasisStack.set(key, stack);
        }
        i = end;
      } else i++;
    }
    return {codes, brackets, parentheses, emphasis};
  }

  function inline(text, context, depth = 0, links = true) {
    if (depth >= MAX_DEPTH) return escape(text);
    const matches = delimiters(text), parts = [];
    let start = 0, i = 0;
    function insert(html, end) { parts.push(escape(text.slice(start, i)), html); i = end; start = i; }
    while (i < text.length) {
      const char = text[i];
      if (char === '\\' && punctuation(text[i + 1])) { insert(escape(text[i + 1]), i + 2); continue; }
      const code = matches.codes.get(i);
      if (code) {
        let value = text.slice(i + code.size, code.end).replace(/\n/g, ' ');
        if (value.startsWith(' ') && value.endsWith(' ') && value.trim()) value = value.slice(1, -1);
        insert('<code>' + escape(value) + '</code>', code.end + code.size); continue;
      }
      const image = char === '!' && text[i + 1] === '[';
      const bracketStart = image ? i + 1 : i;
      if (char === '[' || image) {
        const close = matches.brackets.get(bracketStart);
        if (close !== undefined) {
          const label = text.slice(bracketStart + 1, close);
          const targetEnd = text[close + 1] === '(' ? matches.parentheses.get(close + 1) : undefined;
          if (targetEnd !== undefined) {
            if (!image && links && context.links < 500) {
              const href = safeUrl(text.slice(close + 2, targetEnd).trim());
              if (href) {
                context.links++;
                insert('<a href="' + escape(href) + '" target="_blank" rel="noopener noreferrer">' + inline(label, context, depth + 1, false) + '</a>', targetEnd + 1); continue;
              }
            }
            insert(escape(text.slice(i, targetEnd + 1)), targetEnd + 1); continue;
          }
          if (image) { insert(escape(text.slice(i, close + 1)), close + 1); continue; }
          const filter = links && /^E\d{1,8}$/.test(label) && context.evidence.get(label);
          if (filter && context.references < 200) {
            context.references++;
            insert('<button type="button" class="aa-inline-evidence" data-aa-action="drill" data-evidence="' + escape(label) + '" data-filter="' + escape(JSON.stringify(filter)) + '" aria-label="' + (context.english ? 'View evidence ' : '查看依据 ') + escape(label) + '">[' + escape(label) + ']</button>', close + 1); continue;
          }
        }
      }
      const emphasis = matches.emphasis.get(i);
      if (emphasis) {
        const value = inline(text.slice(i + emphasis.size, emphasis.end), context, depth + 1, links);
        const html = emphasis.size === 1 ? '<em>' + value + '</em>' : emphasis.size === 2 ? '<strong>' + value + '</strong>' : '<strong><em>' + value + '</em></strong>';
        insert(html, emphasis.end + emphasis.size); continue;
      }
      i++;
    }
    parts.push(escape(text.slice(start)));
    return parts.join('');
  }

  function cells(line) {
    let value = line.trim();
    if (value[0] === '|') value = value.slice(1);
    if (value.endsWith('|') && !value.endsWith('\\|')) value = value.slice(0, -1);
    const result = [], ticks = delimiters(value).codes;
    let start = 0;
    for (let i = 0; i < value.length; i++) {
      if (value[i] === '\\') { i++; continue; }
      const code = ticks.get(i);
      if (code) { i = code.end + code.size - 1; continue; }
      if (value[i] === '|') { result.push(value.slice(start, i).trim()); start = i + 1; }
    }
    result.push(value.slice(start).trim());
    return result;
  }

  const fence = line => /^ {0,3}(`{3,}|~{3,})([^\r\n]*)$/.exec(line);
  function heading(line) {
    const prefix = /^ {0,3}(#{1,6})[ \t]+/.exec(line);
    if (!prefix) return null;
    let content = line.slice(prefix[0].length).trim();
    if (!content) return null;
    let hashStart = content.length;
    while (hashStart && content[hashStart - 1] === '#') hashStart--;
    if (hashStart < content.length && hashStart && space(content[hashStart - 1])) content = content.slice(0, hashStart).trimEnd();
    return [line, prefix[1], content];
  }
  const listItem = line => /^( {0,12})([-+*]|\d{1,9}[.)])[ \t]+(.*)$/.exec(line);
  const quote = line => /^ {0,3}>[ \t]?(.*)$/.exec(line);
  const rule = line => /^ {0,3}(?:\*[ \t]*){3,}$/.test(line) || /^ {0,3}(?:-[ \t]*){3,}$/.test(line) || /^ {0,3}(?:_[ \t]*){3,}$/.test(line);
  function tableAt(lines, index) {
    if (index + 1 >= lines.length || !lines[index].includes('|')) return null;
    const titles = cells(lines[index]), alignment = cells(lines[index + 1]);
    if (titles.length > 32 || titles.length !== alignment.length || !alignment.every(value => /^:?-{3,}:?$/.test(value))) return null;
    return {titles, alignment:alignment.map(value => value.startsWith(':') && value.endsWith(':') ? 'center' : value.endsWith(':') ? 'right' : 'left')};
  }
  function blockStart(lines, index) {
    const line = lines[index];
    return !line.trim() || fence(line) || heading(line) || listItem(line) || quote(line) || rule(line) || tableAt(lines, index);
  }
  function paragraph(lines, context) {
    return '<p>' + lines.map((line, index) => {
      const spaces = line.endsWith('  '), slash = line.endsWith('\\') && line[line.length - 2] !== '\\';
      const hard = spaces || slash;
      let end = line.length;
      if (spaces) while (end && line[end - 1] === ' ') end--;
      else if (slash) end--;
      const value = line.slice(0, end);
      return inline(value, context) + (index < lines.length - 1 ? hard ? '<br>' : '\n' : '');
    }).join('') + '</p>';
  }
  function blocks(lines, context, depth = 0) {
    const output = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index++; continue; }
      if (context.blocks >= MAX_BLOCKS || depth >= MAX_DEPTH) {
        output.push('<pre class="aa-rich-overflow">' + escape(lines.slice(index).join('\n')) + '</pre>'); break;
      }
      context.blocks++;
      const opening = fence(line);
      if (opening) {
        const content = [], marker = opening[1][0], size = opening[1].length;
        index++;
        while (index < lines.length) {
          const closing = fence(lines[index]);
          if (closing && closing[1][0] === marker && closing[1].length >= size && !closing[2].trim()) { index++; break; }
          content.push(lines[index++]);
        }
        const language = opening[2].trim();
        output.push('<pre class="aa-code"><code' + (/^[a-zA-Z0-9_-]{1,32}$/.test(language) ? ' class="language-' + language + '"' : '') + '>' + escape(content.join('\n')) + '</code></pre>'); continue;
      }
      const title = heading(line);
      if (title) { const level = Math.min(5, title[1].length + 2); output.push('<h' + level + '>' + inline(title[2], context) + '</h' + level + '>'); index++; continue; }
      if (index + 1 < lines.length && line.trim() && /^ {0,3}(?:=+|-{3,})\s*$/.test(lines[index + 1])) {
        const level = lines[index + 1].trim()[0] === '=' ? 3 : 4;
        output.push('<h' + level + '>' + inline(line, context) + '</h' + level + '>'); index += 2; continue;
      }
      if (rule(line)) { output.push('<hr>'); index++; continue; }
      const table = tableAt(lines, index);
      if (table) {
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes('|') && rows.length < 300 && context.blocks < MAX_BLOCKS) {
          rows.push(cells(lines[index++])); context.blocks++;
        }
        const align = cell => ' class="aa-cell-' + cell + '"';
        output.push('<div class="aa-table-wrap" role="region" tabindex="0" aria-label="' + (context.english ? 'Data table' : '数据表格') + '"><table><thead><tr>' + table.titles.map((value, col) => '<th scope="col"' + align(table.alignment[col]) + '>' + inline(value, context) + '</th>').join('') + '</tr></thead><tbody>' + rows.map(row => '<tr>' + table.titles.map((_, col) => '<td' + align(table.alignment[col]) + '>' + inline(row[col] || '', context) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>'); continue;
      }
      if (quote(line)) {
        const quoted = [];
        while (index < lines.length && quote(lines[index])) quoted.push(quote(lines[index++])[1]);
        output.push('<blockquote>' + blocks(quoted, context, depth + 1) + '</blockquote>'); continue;
      }
      const first = listItem(line);
      if (first) {
        const base = first[1].length, ordered = /^\d/.test(first[2]), tag = ordered ? 'ol' : 'ul', items = [];
        while (index < lines.length && context.blocks < MAX_BLOCKS) {
          const item = listItem(lines[index]);
          if (!item || item[1].length !== base || /^\d/.test(item[2]) !== ordered) break;
          const content = [item[3]], contentIndent = base + item[2].length + 1;
          context.blocks++; index++;
          while (index < lines.length) {
            const next = lines[index], nextItem = listItem(next);
            if (nextItem && nextItem[1].length === base) break;
            const indent = /^ */.exec(next)[0].length;
            if (!next.trim()) {
              const after = lines[index + 1];
              if (after === undefined || (/^ */.exec(after)[0].length <= base && !listItem(after))) break;
              content.push(''); index++; continue;
            }
            if (indent <= base) break;
            content.push(next.slice(Math.min(contentIndent, indent))); index++;
          }
          items.push('<li>' + blocks(content, context, depth + 1) + '</li>');
        }
        const start = ordered ? parseInt(first[2], 10) : 1;
        output.push('<' + tag + (ordered && start !== 1 ? ' start="' + start + '"' : '') + '>' + items.join('') + '</' + tag + '>'); continue;
      }
      const prose = [line]; index++;
      while (index < lines.length && !blockStart(lines, index)) {
        if (index + 1 < lines.length && /^ {0,3}(?:=+|-{3,})\s*$/.test(lines[index + 1])) break;
        prose.push(lines[index++]);
      }
      output.push(paragraph(prose, context));
    }
    return output.join('');
  }

  function render(text, options = {}) {
    options = options || {};
    const source = (typeof text === 'string' ? text : text == null ? '' : String(text)).slice(0, MAX_INPUT).replace(/\r\n?/g, '\n').replace(/\u0000/g, '\ufffd');
    const context = {evidence:evidenceMap(options.evidence), english:options.language === 'en-US', blocks:0, references:0, links:0};
    return blocks(source.split('\n'), context);
  }

  return Object.freeze({render});
});
