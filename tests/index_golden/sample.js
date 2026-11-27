const path = require('./paths');

class Loader extends Base {
  load(name) {
    if (!name) {
      throw new Error("missing module name");
    }
    return path.resolve(name);
  }
}

function boot() {
  return new Loader().load('main');
}

module.exports = { boot };
