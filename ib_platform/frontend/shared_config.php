<?php
/**
 * IronBridge Platform — Shared Configuration
 * 
 * Include in any HTML page via:
 *   <script src="shared_config.php"></script>
 * 
 * Or PHP-include for server-side values:
 *   <?php require 'shared_config.php'; ?>
 *   <script>/* client-side IBConfig is now available */</script>
 */

// Server-side defaults (override via environment variable if needed)
$DEFAULT_API = $_ENV['IRONBRIDGE_API_BASE'] ?? $_SERVER['IRONBRIDGE_API_BASE'] ?? 'http://localhost:8000';

// Sanitize for JS output
$js_api_base = json_encode($DEFAULT_API);
?>
<script>
(function() {
  'use strict';
  
  const DEFAULT_API = <?php echo $js_api_base; ?>;
  
  window.IBConfig = {
    getApiBase() {
      return localStorage.getItem('ib_api_base') || DEFAULT_API;
    },
    setApiBase(url) {
      if (!url) return;
      localStorage.setItem('ib_api_base', url.replace(/\/+$/, ''));
    },
    async healthCheck() {
      try {
        const r = await fetch(this.getApiBase() + '/health');
        return r.ok ? await r.json() : null;
      } catch {
        return null;
      }
    }
  };

  // Allow ?api=http://... override via URL for easy testing
  const params = new URLSearchParams(location.search);
  if (params.has('api')) {
    IBConfig.setApiBase(params.get('api'));
  }
})();
</script>