const { createStorage } = require("./storage");

// Keep the existing HTTP call sites and image-key contract unchanged.
module.exports = createStorage();
