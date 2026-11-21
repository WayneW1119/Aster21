/* Message presentation only: stored messages and MCP always retain the source. */
(() => {
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const parser = typeof window.markdownit === 'function'
    ? window.markdownit({html:false, breaks:true, linkify:true, typographer:false, maxNesting:20})
    : null;
  if (parser) {
    const validate = parser.validateLink;
    parser.validateLink = url => validate(url) && (!/^[a-z][a-z\d+.-]*:/i.test(url) || /^(https?:|mailto:|tel:)/i.test(url));
    const link = parser.renderer.rules.link_open || ((tokens, index, options, env, renderer) => renderer.renderToken(tokens, index, options));
    parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet('target', '_blank');
      tokens[index].attrSet('rel', 'noopener noreferrer');
      return link(tokens, index, options, env, renderer);
    };
    // Do not make third-party requests merely because a message includes an image.
    parser.renderer.rules.image = (tokens, index) => {
      const token = tokens[index], href = token.attrGet('src');
      const label = token.content || '图片';
      return parser.validateLink(href)
        ? `<a class="image-link" href="${escape(href)}" target="_blank" rel="noopener noreferrer">图片：${escape(label)}</a>`
        : escape(label);
    };
    parser.renderer.rules.fence = (tokens, index) => {
      const token = tokens[index], language = token.info.trim().split(/\s+/)[0].slice(0,40);
      return `<div class="code-block"><div class="code-toolbar"><span>${escape(language || '代码')}</span><button type="button" class="copy-code" aria-label="复制代码">复制</button></div><pre><code>${escape(token.content)}</code></pre></div>\n`;
    };
    parser.renderer.rules.code_block = parser.renderer.rules.fence;
    parser.renderer.rules.table_open = () => '<div class="markdown-table" tabindex="0" role="region" aria-label="消息表格"><table>\n';
    parser.renderer.rules.table_close = () => '</table></div>\n';
  }
  window.renderRelayMarkdown = value => {
    const source = String(value ?? '');
    try {
      if (parser) return parser.render(source);
    } catch (_) { /* A malformed message must not hide the rest of the conversation. */ }
    return `<span class="markdown-plain">${escape(source)}</span>`;
  };
})();
