const fs = require("node:fs/promises");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const uploadDir = path.join(process.cwd(), "uploads");

async function saveImage(buffer, extension) {
  await fs.mkdir(uploadDir, { recursive: true });
  const key = `${randomUUID()}.${extension}`;
  await fs.writeFile(path.join(uploadDir, key), buffer);
  return key;
}

async function readImage(key) {
  const validKey =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(png|jpg)$/;

  if (!validKey.test(key)) {
    return null;
  }

  try {
    return await fs.readFile(path.join(uploadDir, key));
  } catch (error) {
    if (error.code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

module.exports = { saveImage, readImage };