module.exports = {
  apps: [
    {
      name: "custom-avrae",
      cwd: "/home/joe/dev/Bots/avrae",
      script: "dbot.py",
      interpreter: "./.venv/bin/python",
      args: "test",
    },
  ],
};
