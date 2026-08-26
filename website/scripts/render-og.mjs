import { copyFile, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import sharp from "sharp";

const directory = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(directory, "../assets/og.svg");
const output = path.resolve(directory, "../public/og-twelve-harnesses.png");
const legacyOutput = path.resolve(directory, "../public/og.png");
const svg = await readFile(source);

await sharp(svg, { density: 144 })
  .resize(1731, 909, { fit: "fill" })
  .png({ compressionLevel: 9, palette: false })
  .toFile(output);
await copyFile(output, legacyOutput);

console.log(`Rendered ${path.relative(process.cwd(), output)}`);
