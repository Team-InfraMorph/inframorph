require("dotenv").config();

const { PrismaClient } = require("@prisma/client");

const prisma = new PrismaClient();

async function tick() {
  try {
    const noteCount = await prisma.note.count();
    console.log(JSON.stringify({
      event: "note_count",
      count: noteCount,
      at: new Date().toISOString(),
    }));
  } catch (error) {
    console.error(error);
    process.exit(1);
  }
}

const timer = setInterval(tick, 10_000);
tick();

process.on("SIGTERM", async () => {
  clearInterval(timer);
  await prisma.$disconnect();
  process.exit(0);
});