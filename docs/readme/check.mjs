import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const assets = join(root, "assets", "readme");
const previews = join(root, "docs", "readme", "previews");
const wideFonts = "text{font-family:Verdana,'DejaVu Sans',sans-serif!important}";

function launch() {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined;
  return chromium.launch({ executablePath });
}

function staticProblems(name, source) {
  const problems = [];
  if (!/^<svg[^>]*\bviewBox="0 0 \d+ \d+"/.test(source)) problems.push("missing viewBox");
  if (!/role="img"/.test(source)) problems.push("missing role=img");
  if (!/<title[^>]*>[^<]+<\/title>/.test(source)) problems.push("missing <title>");
  if (/(href|src)="https?:/.test(source) || /url\(\s*['"]?https?:/.test(source)) problems.push("external reference");
  if (/@import|@font-face/.test(source)) problems.push("remote font or import");
  if (/animation/.test(source) && !/prefers-reduced-motion/.test(source)) problems.push("animation without reduced-motion fallback");
  if (/<script|on[a-z]+="/.test(source)) problems.push("script or event handler");
  if (/(api[_-]?key|secret|password|token)\s*[:=]/i.test(source)) problems.push("credential-like text");
  return problems.map((problem) => `${name}: ${problem}`);
}

async function layoutProblems(page, name, source, extraCss) {
  await page.setContent(
    `<!doctype html><html><head><style>html,body{margin:0;background:#fff}${extraCss}</style></head><body>${source}</body></html>`,
  );
  return page.evaluate((label) => {
    const problems = [];
    const svg = document.querySelector("svg");
    const parserError = document.querySelector("parsererror");
    if (parserError) return [`${label}: XML parse error`];
    const view = svg.getBoundingClientRect();
    const texts = [...svg.querySelectorAll("text")].filter((node) => !node.closest("[data-check-skip]"));
    const boxes = texts.map((node) => ({ node, box: node.getBoundingClientRect() }));
    const inset = (outer, inner, pad) =>
      inner.left >= outer.left - pad && inner.right <= outer.right + pad && inner.top >= outer.top - pad && inner.bottom <= outer.bottom + pad;
    for (const { node, box } of boxes) {
      const text = node.textContent.trim().slice(0, 40);
      if (box.width === 0) continue;
      if (!inset(view, box, 0.5)) problems.push(`${label}: text clipped by the canvas: "${text}"`);
      const container = node.getAttribute("data-in");
      if (container) {
        const frame = svg.querySelector(`#${container}`);
        if (!frame) problems.push(`${label}: missing container #${container}`);
        else if (!inset(frame.getBoundingClientRect(), box, 1)) problems.push(`${label}: text leaves #${container}: "${text}"`);
      }
    }
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i].box;
        const b = boxes[j].box;
        if (a.width === 0 || b.width === 0) continue;
        const overlapX = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const overlapY = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        const block = boxes[i].node.getAttribute("data-lines");
        const size = Number(boxes[i].node.getAttribute("font-size")) || 0;
        const baselineGap = Math.abs(Number(boxes[i].node.getAttribute("y")) - Number(boxes[j].node.getAttribute("y")));
        const stackedLines = block && block === boxes[j].node.getAttribute("data-lines") && baselineGap >= size * 0.9;
        if (overlapX > 2 && overlapY > 2 && !stackedLines) {
          problems.push(
            `${label}: text overlaps: "${boxes[i].node.textContent.trim().slice(0, 30)}" / "${boxes[j].node.textContent.trim().slice(0, 30)}"`,
          );
        }
      }
    }
    return problems;
  }, name);
}

async function renderAssets(browser, files) {
  mkdirSync(previews, { recursive: true });
  const context = await browser.newContext({ reducedMotion: "reduce", deviceScaleFactor: 1 });
  const page = await context.newPage();
  const problems = [];
  for (const file of files) {
    const name = basename(file);
    const source = readFileSync(file, "utf8");
    problems.push(...staticProblems(name, source));
    const [, width, height] = source.match(/viewBox="0 0 (\d+) (\d+)"/) || [];
    await page.setViewportSize({ width: Number(width) || 1200, height: Number(height) || 600 });
    problems.push(...(await layoutProblems(page, name, source, "")));
    await page.screenshot({ path: join(previews, name.replace(/\.svg$/, ".png")) });
    problems.push(...(await layoutProblems(page, `${name} [wide fallback fonts]`, source, wideFonts)));
  }
  await context.close();
  return problems;
}

function readmeHtml() {
  const markdown = readFileSync(join(root, "README.md"), "utf8");
  return execFileSync("gh", ["api", "markdown", "-f", `text=${markdown}`, "-f", "mode=markdown", "-f", "context=maskentir/fakeyou"], {
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  });
}

async function renderReadme(browser) {
  const html = readmeHtml();
  const base = pathToFileURL(`${root}/`).href;
  const shots = [
    ["desktop-light", 1280, "light"],
    ["desktop-dark", 1280, "dark"],
    ["mobile-light", 390, "light"],
    ["mobile-dark", 390, "dark"],
  ];
  const broken = new Set();
  for (const [label, width, scheme] of shots) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, reducedMotion: "reduce", deviceScaleFactor: 1 });
    const page = await context.newPage();
    const background = scheme === "dark" ? "#0d1117" : "#ffffff";
    const color = scheme === "dark" ? "#e6edf3" : "#1f2328";
    const document = join(previews, `.readme-${label}.html`);
    writeFileSync(
      document,
      `<!doctype html><html><head><meta charset="utf-8"><base href="${base}"><style>
        body{margin:0;background:${background};color:${color};font:16px/1.5 -apple-system,'Segoe UI',Helvetica,Arial,sans-serif}
        article{box-sizing:border-box;max-width:${width > 800 ? "980px" : "100%"};margin:0 auto;padding:${width > 800 ? "32px" : "16px"}}
        article{overflow-wrap:break-word}img{max-width:100%}pre{overflow:auto;padding:16px;border-radius:6px;background:${scheme === "dark" ? "#151b23" : "#f6f8fa"}}
        code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:85%}table{border-collapse:collapse;display:block;overflow:auto}
        td,th{border:1px solid ${scheme === "dark" ? "#3d444d" : "#d1d9e0"};padding:6px 13px}a{color:${scheme === "dark" ? "#4493f8" : "#0969da"}}
        h2{border-bottom:1px solid ${scheme === "dark" ? "#3d444d" : "#d1d9e0"};padding-bottom:.3em}
      </style></head><body><article>${html}</article></body></html>`,
    );
    await page.goto(pathToFileURL(document).href, { waitUntil: "load" });
    rmSync(document);
    await page.evaluate(async () => {
      for (let y = 0; y < document.body.scrollHeight; y += window.innerHeight / 2) {
        window.scrollTo(0, y);
        await new Promise((resolve) => setTimeout(resolve, 120));
      }
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(3000);
    for (const missing of await page.evaluate(() =>
      [...document.images].filter((image) => !image.complete || image.naturalWidth === 0).map((image) => image.getAttribute("src")),
    )) {
      broken.add(missing);
    }
    await page.screenshot({ path: join(previews, `readme-${label}.jpg`), type: "jpeg", quality: 72, fullPage: true });
    await context.close();
  }
  return [...broken].map((source) => `README.md: broken image ${source}`);
}

async function main() {
  const readme = process.argv.includes("--readme");
  const files = readdirSync(assets)
    .filter((name) => name.endsWith(".svg"))
    .sort()
    .map((name) => join(assets, name));
  const browser = await launch();
  try {
    const problems = await renderAssets(browser, files);
    if (readme) problems.push(...(await renderReadme(browser)));
    const report = { assets: files.length, problems };
    writeFileSync(join(previews, "check-report.json"), `${JSON.stringify(report, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    process.exitCode = problems.length ? 1 : 0;
  } finally {
    await browser.close();
  }
}

await main();
