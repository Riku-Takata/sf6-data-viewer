/**
 * SuperCombo Wiki ゲームシステム文書 取得スクリプト
 *
 * 使い方:
 *   1. ブラウザで https://wiki.supercombo.gg/w/SF6/Gauges を開く
 *   2. 開発者コンソール (F12 → Console) を開く
 *   3. このスクリプト全体をコピーして貼り付け、Enter を押す
 *   4. しばらく待つと JSON ファイルが自動ダウンロードされる
 *   5. ダウンロードしたファイルを docs/ ディレクトリに保存する
 *
 * 注意:
 *   - Cloudflare 対策のためブラウザから実行する必要がある
 *   - 1秒間隔でリクエストするため約30秒かかる
 */

const PAGES = [
  // 高優先度 (M2 で必須)
  { slug: 'Gauges',    title: 'Drive Gauge / Super Gauge / Burnout' },
  { slug: 'Offense',   title: 'Counter Hit / Punish Counter / Drive Impact' },
  { slug: 'Defense',   title: 'Block / Drive Parry / Drive Reversal' },
  // 中優先度
  { slug: 'Movement',  title: 'Walk / Dash / Jump / Drive Rush' },
  { slug: 'Controls',  title: 'Input / Modern / Classic' },
  // 低優先度
  { slug: 'HUD',       title: 'Health bar / Timer / Round system' },
  { slug: 'Glossary',  title: '用語集' },
];

// ⚠ 修正: SF6 → Street_Fighter_6 (実際のWikiのURL構造)
const BASE_URL = 'https://wiki.supercombo.gg/w/Street_Fighter_6';

async function fetchPage(slug) {
  const url = `${BASE_URL}/${slug}`;
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${url}`);
  const html = await resp.text();

  // DOMParser でメインコンテンツのみ抽出
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const content = doc.querySelector('#mw-content-text .mw-parser-output');
  if (!content) return html; // フォールバック: ページ全体

  // ナビゲーション・目次・広告を除去
  content.querySelectorAll('.navbox, .toc, .mw-editsection, #toc').forEach(el => el.remove());

  return content.innerHTML;
}

async function main() {
  const results = {
    fetched_at: new Date().toISOString(),
    base_url: BASE_URL,
    pages: {}
  };

  for (const { slug, title } of PAGES) {
    console.log(`取得中: SF6/${slug} (${title}) ...`);
    try {
      const html = await fetchPage(slug);
      results.pages[`SF6/${slug}`] = { title, html, slug };
      console.log(`  ✅ SF6/${slug}: ${html.length} chars`);
    } catch (e) {
      console.error(`  ❌ SF6/${slug}: ${e.message}`);
      results.pages[`SF6/${slug}`] = { title, html: '', slug, error: e.message };
    }
    // Cloudflare 対策 + サーバー負荷軽減のための待機
    await new Promise(r => setTimeout(r, 1500));
  }

  // JSON としてダウンロード
  const json = JSON.stringify(results, null, 2);
  const blob = new Blob([json], { type: 'application/json' });
  const a = document.createElement('a');
  const date = new Date().toISOString().slice(0, 10);
  a.href = URL.createObjectURL(blob);
  a.download = `supercombo_docs_${date}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  console.log(`\n✅ ダウンロード完了: supercombo_docs_${date}.json`);
  console.log(`取得ページ数: ${Object.keys(results.pages).length}`);
}

main().catch(console.error);
