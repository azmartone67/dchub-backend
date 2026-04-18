/**
 * DC Hub API Configuration
 * Deploy this file to Cloudflare Pages at: /static/js/api-config.js
 * 
 * This sets the backend URL so the dashboard knows where to send API requests.
 * Without this file, DCHUB_API_BASE defaults to '' and all API calls
 * go to dchub.cloud (Cloudflare) instead of the Replit backend.
 */
window.DCHUB_API_BASE = 'https://dchub.cloud';
