// 自定义部署时可指定 API 地址，例如 'https://api.example.com'。空字符串使用当前站点。
window.YUN_API_BASE_URL = ['http://127.0.0.1:8000', 'http://localhost:8000'].includes(location.origin)
  ? 'http://127.0.0.1:8765'
  : '';
