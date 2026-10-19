// pm2 守护配置：memory MCP 常驻服务（streamable-http，0.0.0.0:8766/mcp，局域网 192.168.5.x 与 Tailscale 可直连）
// 启动：pm2 start /root/memory/ecosystem.config.js
module.exports = {
  apps: [
    {
      name: "memory-mcp",
      script: "server.py",
      interpreter: "/root/memory/.venv/bin/python",
      args: "serve",
      cwd: "/root/memory",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 3000,
      max_memory_restart: "400M",
      env: {
        MEMORY_HTTP_HOST: "0.0.0.0",
        MEMORY_HTTP_PORT: "8766",
      },
      out_file: "/root/.pm2/logs/memory-mcp-out.log",
      error_file: "/root/.pm2/logs/memory-mcp-error.log",
      merge_logs: true,
      time: true,
    },
  ],
};
