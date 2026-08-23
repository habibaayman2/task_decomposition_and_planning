<?php
$DEFAULT_API = $_ENV['IRONBRIDGE_API_BASE'] ?? $_SERVER['IRONBRIDGE_API_BASE'] ?? 'http://localhost:8000';
$js_api_base = json_encode($DEFAULT_API);

// Check if included directly as <script src="...">
// If so, output pure JS without <script> tags
$is_script_src = (basename($_SERVER['PHP_SELF']) === 'shared_config.php');
?>

<?php if (!$is_script_src): ?><script><?php endif; ?>

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

const params = new URLSearchParams(location.search);
if (params.has('api')) {
IBConfig.setApiBase(params.get('api'));
}
})();

<?php if (!$is_script_src): ?></script><?php endif; ?>