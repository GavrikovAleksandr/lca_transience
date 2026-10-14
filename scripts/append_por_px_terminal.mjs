/**
 * Append one purified terminal-import LCI without re-exporting existing cells.
 *
 * The new block is authored/exported with the bundled artifact-tool. A narrow
 * OOXML row merge then preserves every pre-existing row and other ZIP part,
 * including formula caches, styles, comments, drawings and workbook metadata.
 * Importing this module performs no filesystem work. Run --inspect, review the
 * original preview, run --prepare, review the candidate preview, then --apply.
 */
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const INPUT = path.join(REPO, "inventories", "lci-itom_por.xlsx");
const WORK = path.join(REPO, "tmp", "por_px_purity");
const DEPS = "C:/Users/terlouw_t/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules";
const NODE = "C:/Users/terlouw_t/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe";
const MARKER = "C:/Users/terlouw_t/.codex/plugins/cache/openai-primary-runtime/spreadsheets/26.1011.11113/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs";
const SHEET_PART = "xl/worksheets/sheet1.xml";
const ACTIVITY = "p-xylene, purified, imported from outside EU (por)";
const TEMPLATE = "raw p-xylene, imported from outside EU (por)";
const COMMENT = "Purified terminal import per ITOM clarification (Svenja). Locally used origin shares are proportional to inflows; no additional PoR purification is added.";
const SOURCE = "ITOM clarification from Svenja: all terminal imports are purified. Existing ecoinvent market for p-xylene (RoW) supplies the upstream LCI.";

function sha(value) { return crypto.createHash("sha256").update(value).digest("hex"); }
function assert(condition, message) { if (!condition) throw new Error(message); }
function escapeXml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&apos;");
}
function decodeXml(value) {
  return value.replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", '"')
    .replaceAll("&apos;", "'").replaceAll("&amp;", "&");
}
function attr(xml, name) { return xml.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1]; }
function rows(xml) { return [...xml.matchAll(/<row\b[^>]*\/>|<row\b[^>]*>[\s\S]*?<\/row>/g)].map(x => x[0]); }
function cells(row) { return [...row.matchAll(/<c\b[^>]*\/>|<c\b[^>]*>[\s\S]*?<\/c>/g)].map(x => x[0]); }
function sharedStrings(xml = "") {
  return [...xml.matchAll(/<si\b[^>]*>([\s\S]*?)<\/si>/g)]
    .map(x => [...x[1].matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)].map(y => decodeXml(y[1])).join(""));
}
function cellValue(cell, strings) {
  const value = cell.match(/<v\b[^>]*>([\s\S]*?)<\/v>/)?.[1];
  if (attr(cell, "t") === "s") return strings[Number(value)];
  if (attr(cell, "t") === "inlineStr") {
    return [...cell.matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)].map(x => decodeXml(x[1])).join("");
  }
  return value === undefined ? null : decodeXml(value);
}
function rowValues(row, strings) {
  return Object.fromEntries(cells(row).map(cell => [attr(cell, "r").replace(/\d+$/, ""), cellValue(cell, strings)]));
}

async function runtime() {
  await fs.mkdir(WORK, { recursive: true });
  const junction = path.join(WORK, "node_modules");
  try { await fs.stat(junction); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    await fs.symlink(DEPS, junction, "junction");
  }
  assert((await fs.realpath(junction)).toLowerCase() === (await fs.realpath(DEPS)).toLowerCase(),
    "Authoring must use the configured bundled dependencies, not repo-local packages");
  const require = createRequire(path.join(WORK, "bundled-runtime.cjs"));
  const artifact = await import(pathToFileURL(require.resolve("@oai/artifact-tool")).href);
  return { ...artifact, JSZip: require("jszip") };
}

async function readInventory(JSZip, input = INPUT) {
  const bytes = await fs.readFile(input);
  const zip = await JSZip.loadAsync(bytes, { createFolders: false });
  const partBytes = {};
  for (const [name, item] of Object.entries(zip.files)) if (!item.dir) partBytes[name] = await item.async("nodebuffer");
  assert(partBytes[SHEET_PART], "Expected the unchanged inventory sheet1.xml");
  const xml = partBytes[SHEET_PART].toString("utf8");
  const strings = sharedStrings(partBytes["xl/sharedStrings.xml"]?.toString("utf8"));
  const allRows = rows(xml);
  const header = allRows.find(row => { const values = rowValues(row, strings); return values.A === "Activity" && values.B === TEMPLATE; });
  assert(header, "The existing raw outside-EU p-xylene template was not found");
  const templateStart = Number(attr(header, "r"));
  const templateRows = allRows.filter(row => Number(attr(row, "r")) >= templateStart && Number(attr(row, "r")) <= templateStart + 8);
  assert(templateRows.length === 9, "The original wrapper layout changed; review before appending");
  const provider = rowValues(templateRows[8], strings);
  assert(provider.A === "market for p-xylene" && provider.B === "1" && provider.C === "RoW"
    && provider.D === "kilogram" && provider.F === "technosphere" && provider.G === "p-xylene",
  "The existing wrapper no longer confirms the expected RoW p-xylene provider");
  const present = allRows.filter(row => { const values = rowValues(row, strings); return values.A === "Activity" && values.B === ACTIVITY; });
  assert(present.length <= 1, "Duplicate purified terminal-import activity already exists");
  if (present.length) {
    const start = Number(attr(present[0], "r"));
    const block = allRows.filter(row => Number(attr(row, "r")) >= start && Number(attr(row, "r")) <= start + 9)
      .map(row => rowValues(row, strings));
    assert(block.length === 10 && block[1].B === "p-xylene" && block[2].B === "kilogram" && block[3].B === "RER",
      "Existing purified terminal wrapper differs from the declared metadata");
    assert(block[8].A === ACTIVITY && Number(block[8].B) === 1 && block[8].C === "RER"
      && block[8].D === "kilogram" && block[8].F === "production" && block[8].G === "p-xylene",
    "Existing purified terminal wrapper has a different production exchange");
    assert(block[9].A === "market for p-xylene" && Number(block[9].B) === 1 && block[9].C === "RoW"
      && block[9].D === "kilogram" && block[9].F === "technosphere" && block[9].G === "p-xylene",
    "Existing purified terminal wrapper has a different supply exchange");
  }
  const formulaXml = [...xml.matchAll(/<f\b[^>]*\/>|<f\b[^>]*>[\s\S]*?<\/f>/g)].map(x => x[0]);
  return { bytes, zip, partBytes, xml, strings, allRows, templateRows, templateStart,
    presentStart: present.length ? Number(attr(present[0], "r")) : null,
    maxRow: Math.max(...allRows.map(row => Number(attr(row, "r")))), formulaXml };
}

async function savePreview(workbook, range, filename) {
  const preview = await workbook.render({ sheetName: "Sheet1", range, scale: 1, format: "png" });
  await fs.writeFile(path.join(WORK, filename), new Uint8Array(await preview.arrayBuffer()));
}

export async function inspectInventory(input = INPUT) {
  const { JSZip, SpreadsheetFile, FileBlob } = await runtime();
  const original = await readInventory(JSZip, input);
  const book = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
  const range = `A${original.templateStart}:P${original.templateStart + 8}`;
  console.log((await book.inspect({ kind: "table", sheetId: "Sheet1", range,
    include: "values,formulas", maxChars: 4500, tableMaxRows: 9, tableMaxCols: 8 })).ndjson);
  await savePreview(book, range, "original_wrapper.png");
  const snapshot = { input: path.resolve(input), input_sha256: sha(original.bytes),
    template_rows: [original.templateStart, original.templateStart + 8],
    max_row: original.maxRow, old_row_count: original.allRows.length,
    formula_count: original.formulaXml.length, present_start: original.presentStart,
    part_sha256: Object.fromEntries(Object.entries(original.partBytes).map(([name, bytes]) => [name, sha(bytes)])) };
  await fs.writeFile(path.join(WORK, "input_snapshot.json"), JSON.stringify(snapshot, null, 2));
  console.log(JSON.stringify({ ...snapshot, preview: path.join(WORK, "original_wrapper.png") }));
  return snapshot;
}

function convertAuthoredRows(stubXml, strings, templateRows, newStart) {
  const authored = rows(stubXml);
  assert(authored.length === 10, "Expected exactly ten authored inventory rows");
  assert(!/<f\b/.test(stubXml), "The new terminal wrapper must not add formulas");
  // New source row uses the ordinary metadata-row style. All other row roles
  // match the existing wrapper; styles.xml is never rewritten or extended.
  const templateOffsets = [0, 1, 2, 3, 4, 4, 5, 6, 7, 8];
  return authored.map((row, index) => {
    const number = newStart + index;
    const styleRow = templateRows[templateOffsets[index]];
    const styles = Object.fromEntries(cells(styleRow).map(cell => [attr(cell, "r").replace(/\d+$/, ""), attr(cell, "s")]));
    const rowStart = styleRow.match(/^<row\b[^>]*>/)[0].replace(/\br="\d+"/, `r="${number}"`);
    const appendedCells = cells(row).map(cell => {
      const column = attr(cell, "r").replace(/\d+$/, "");
      const value = cellValue(cell, strings);
      if (value === null) return ""; // no authoring-only empty styled cells
      const originalType = attr(cell, "t");
      const style = styles[column] === undefined ? "" : ` s="${styles[column]}"`;
      if (originalType === "s" || originalType === "inlineStr" || originalType === "str") {
        return `<c r="${column}${number}"${style} t="inlineStr"><is><t xml:space="preserve">${escapeXml(value)}</t></is></c>`;
      }
      assert(column === "B" && (index === 8 || index === 9) && value === "1", "Unexpected numeric value in the new block");
      return `<c r="${column}${number}"${style}><v>1</v></c>`;
    }).join("");
    return rowStart + appendedCells + "</row>";
  }).join("");
}

export async function prepareAppend(input = INPUT) {
  const { JSZip, SpreadsheetFile, FileBlob, Workbook } = await runtime();
  const original = await readInventory(JSZip, input);
  if (original.presentStart !== null) {
    const result = { status: "already_present", rows: [original.presentStart, original.presentStart + 9], sha256: sha(original.bytes) };
    console.log(JSON.stringify(result));
    return result;
  }
  const snapshot = JSON.parse(await fs.readFile(path.join(WORK, "input_snapshot.json"), "utf8"));
  assert(snapshot.input_sha256 === sha(original.bytes), "Inventory changed after the original preview; inspect it again first");
  const ledgerPath = path.join(WORK, "artifact_operation_started.json");
  let ledger;
  try { ledger = JSON.parse(await fs.readFile(ledgerPath, "utf8")); } catch (error) { if (error.code !== "ENOENT") throw error; }
  if (!ledger || ledger.input_sha256 !== snapshot.input_sha256) {
    // Required marker immediately precedes the first workbook authoring call.
    execFileSync(NODE, [MARKER, "--operation-kind", "edit", "--expected-output-count", "1", "--output-format", "xlsx"], { stdio: "inherit", cwd: REPO });
    await fs.writeFile(ledgerPath, JSON.stringify({ input_sha256: snapshot.input_sha256, operation: "edit", expected_outputs: 1 }));
  }
  const book = Workbook.create();
  const sheet = book.worksheets.add("Sheet1");
  const data = [
    ["Activity", ACTIVITY, null, null, null, null, null, null],
    ["reference product", "p-xylene", null, null, null, null, null, null],
    ["unit", "kilogram", null, null, null, null, null, null],
    ["location", "RER", null, null, null, null, null, null],
    ["comment", COMMENT, null, null, null, null, null, null],
    ["source", SOURCE, null, null, null, null, null, null],
    ["Exchanges", null, null, null, null, null, null, null],
    ["name", "amount", "location", "unit", "categories", "type", "reference product", "comment"],
    [ACTIVITY, 1, "RER", "kilogram", null, "production", "p-xylene", null],
    ["market for p-xylene", 1, "RoW", "kilogram", null, "technosphere", "p-xylene", null],
  ];
  sheet.getRange("A1:H10").values = data;
  sheet.getRange("A1:H10").format.font = { name: "Calibri", size: 11 };
  for (const [column, width] of [["A", 121.15625], ["B", 12.41796875], ["C", 12.15625], ["D", 12.41796875],
    ["E", 36.26171875], ["F", 18.20703125], ["G", 17], ["L", 10.15625], ["N", 11.68359375], ["O", 16.20703125]]) {
    sheet.getRange(`${column}1:${column}10`).format.columnWidth = width;
  }
  for (const range of ["A1:B1", "A7", "A8:H8"]) sheet.getRange(range).format.font = { name: "Calibri", size: 11, bold: true };
  book.recalculate();
  console.log((await book.inspect({ kind: "table", sheetId: "Sheet1", range: "A1:H10",
    include: "values,formulas", maxChars: 4500, tableMaxRows: 10, tableMaxCols: 8 })).ndjson);
  console.log((await book.inspect({ kind: "match", sheetId: "Sheet1", range: "A1:H10",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 20 } })).ndjson);
  const stubPath = path.join(WORK, "authored_new_block.xlsx");
  await (await SpreadsheetFile.exportXlsx(book)).save(stubPath);
  const stubZip = await JSZip.loadAsync(await fs.readFile(stubPath));
  // The artifact exporter uses x: namespaced tags. Normalize only its new
  // temporary block, never the original worksheet/package bytes.
  const stubXml = (await stubZip.file(SHEET_PART).async("string")).replace(/(<\/?)x:/g, "$1");
  const stubStrings = sharedStrings(stubZip.file("xl/sharedStrings.xml")
    ? (await stubZip.file("xl/sharedStrings.xml").async("string")).replace(/(<\/?)x:/g, "$1") : "");
  const start = original.maxRow + 2, end = start + 9;
  const newRows = convertAuthoredRows(stubXml, stubStrings, original.templateRows, start);
  const oldDimension = original.xml.match(/<dimension\b[^>]*ref="([^"]+)"[^>]*\/>/)[0];
  const oldDimensionRef = attr(oldDimension, "ref");
  const endColumn = oldDimensionRef.match(/:([A-Z]+)\d+$/)[1];
  const newDimension = oldDimension.replace(/ref="[^"]+"/, `ref="A1:${endColumn}${end}"`);
  const newXml = original.xml.replace(oldDimension, newDimension).replace("</sheetData>", newRows + "</sheetData>");
  assert(newXml.replace(newRows, "").replace(newDimension, oldDimension) === original.xml,
    "Mechanical merge altered pre-existing worksheet bytes");
  assert(JSON.stringify([...newXml.matchAll(/<f\b[^>]*\/>|<f\b[^>]*>[\s\S]*?<\/f>/g)].map(x => x[0])) === JSON.stringify(original.formulaXml),
    "Existing formulas changed");
  original.zip.file(SHEET_PART, Buffer.from(newXml, "utf8"), { createFolders: false, date: original.zip.file(SHEET_PART).date });
  const candidateBytes = await original.zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE", compressionOptions: { level: 6 } });
  const candidatePath = path.join(WORK, "candidate.xlsx");
  await fs.writeFile(candidatePath, candidateBytes);
  const candidate = await readInventory(JSZip, candidatePath);
  assert(candidate.presentStart === start, "New activity is missing from the candidate");
  assert(Object.keys(candidate.partBytes).length === Object.keys(original.partBytes).length, "Unexpected ZIP part addition/removal");
  const untouched = Object.keys(original.partBytes).filter(name => name !== SHEET_PART);
  for (const name of untouched) assert(candidate.partBytes[name].equals(original.partBytes[name]), `Pre-existing ZIP part changed: ${name}`);
  assert(candidate.formulaXml.length === original.formulaXml.length, "Formula count changed");
  const oldRows = original.allRows;
  for (let index = 0; index < oldRows.length; index++) assert(candidate.allRows[index] === oldRows[index], "An existing row/formula cache changed");
  const candidateBook = await SpreadsheetFile.importXlsx(await FileBlob.load(candidatePath));
  await savePreview(candidateBook, `A${start}:P${end}`, "new_wrapper.png");
  const proof = { status: "prepared", input: path.resolve(input), input_sha256: snapshot.input_sha256,
    candidate: candidatePath, candidate_sha256: sha(candidateBytes), activity: ACTIVITY, new_rows: [start, end],
    original_row_count: oldRows.length, retained_existing_rows_byte_identical: true,
    old_formula_count: original.formulaXml.length, new_formula_count: candidate.formulaXml.length,
    unchanged_zip_parts: untouched, modified_zip_parts: [SHEET_PART],
    sheet_change_scope: "dimension plus ten appended rows only", legacy_raw_wrapper_unchanged: true,
    preview: path.join(WORK, "new_wrapper.png") };
  await fs.writeFile(path.join(WORK, "append_proof.json"), JSON.stringify(proof, null, 2));
  console.log(JSON.stringify(proof));
  return proof;
}

export async function applyAppend(input = INPUT) {
  const proof = JSON.parse(await fs.readFile(path.join(WORK, "append_proof.json"), "utf8"));
  const { JSZip } = await runtime();
  const original = await readInventory(JSZip, input);
  if (original.presentStart !== null) {
    const result = { status: "already_present", rows: [original.presentStart, original.presentStart + 9], sha256: sha(original.bytes) };
    console.log(JSON.stringify(result));
    return result;
  }
  assert(sha(original.bytes) === proof.input_sha256, "Inventory changed since preparation; do not overwrite concurrent edits");
  const candidate = await fs.readFile(proof.candidate);
  assert(sha(candidate) === proof.candidate_sha256, "Candidate changed since visual verification");
  await fs.copyFile(input, path.join(WORK, "original_before_append.xlsx"));
  const destination = path.join(path.dirname(path.resolve(input)), `.por_px_${process.pid}.xlsx`);
  try {
    await fs.writeFile(destination, candidate);
    assert(sha(await fs.readFile(input)) === proof.input_sha256, "Inventory changed immediately before publication");
    await fs.rename(destination, input);
  } finally { await fs.unlink(destination).catch(error => { if (error.code !== "ENOENT") throw error; }); }
  const final = await readInventory(JSZip, input);
  assert(sha(final.bytes) === proof.candidate_sha256 && final.presentStart === proof.new_rows[0], "Published workbook differs from verified candidate");
  const result = { ...proof, status: "appended", final_sha256: sha(final.bytes), backup: path.join(WORK, "original_before_append.xlsx") };
  await fs.writeFile(path.join(WORK, "append_proof.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
  return result;
}

async function main() {
  const arguments_ = process.argv.slice(2);
  assert(arguments_.length === 1, "Usage: append_por_px_terminal.mjs --inspect | --prepare | --apply");
  if (arguments_[0] === "--inspect") await inspectInventory();
  else if (arguments_[0] === "--prepare") await prepareAppend();
  else if (arguments_[0] === "--apply") await applyAppend();
  else throw new Error("Unknown operation; no workbook was changed");
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
}
