require("dotenv").config();

const express = require("express");
const { PrismaClient } = require("@prisma/client");
const { saveImage, readImage } = require("./images");

const app = express();
const prisma = new PrismaClient();

app.use(express.json({ limit: "16kb" }));

function asyncRoute(handler) {
  return (req, res, next) => {
    Promise.resolve(handler(req, res)).catch(next);
  };
}

app.get("/", (req, res) => {
  res.type("text/plain").send("InfraMorph demo v1");
});

app.get("/health", asyncRoute(async (req, res) => {
  await prisma.note.count();
  res.json({ status: "ok" });
}));

app.post("/api/notes", asyncRoute(async (req, res) => {
  const text = req.body && req.body.text;
  if (typeof text !== "string" || !text.trim() || text.length > 500) {
    return res.status(400).json({ error: "text must contain 1-500 characters" });
  }

  const note = await prisma.note.create({
    data: { text: text.trim() },
  });
  res.status(201).json(note);
}));

app.get("/api/notes", asyncRoute(async (req, res) => {
  const notes = await prisma.note.findMany({
    orderBy: { id: "desc" },
  });
  res.json(notes);
}));

app.post(
  "/api/images",
  express.raw({ type: ["image/png", "image/jpeg"], limit: "5mb" }),
  asyncRoute(async (req, res) => {
    const mime = (req.headers["content-type"] || "").split(";")[0];
    if (mime !== "image/png" && mime !== "image/jpeg") {
      return res.status(415).json({ error: "PNG or JPEG required" });
    }
    if (!Buffer.isBuffer(req.body) || req.body.length === 0) {
      return res.status(400).json({ error: "image body required" });
    }

    const extension = mime === "image/png" ? "png" : "jpg";
    const key = await saveImage(req.body, extension);
    res.status(201).json({ key, url: `/api/images/${key}` });
  }),
);

app.get("/api/images/:key", asyncRoute(async (req, res) => {
  const image = await readImage(req.params.key);
  if (image === null) {
    return res.status(404).json({ error: "image not found" });
  }

  res.type(req.params.key.endsWith(".png") ? "png" : "jpeg");
  res.send(image);
}));

app.use((error, req, res, next) => {
  console.error(error);
  const tooLarge = error.type === "entity.too.large";
  res.status(tooLarge ? 413 : 500).json({
    error: tooLarge ? "image too large" : "internal error",
  });
});

const port = Number(process.env.PORT || 3000);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("PORT must be between 1 and 65535");
}

app.listen(port, "0.0.0.0", () => {
  console.log(`demo-app listening on ${port}`);
});