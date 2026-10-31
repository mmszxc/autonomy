import { getJSON } from './api.js';

export async function loadNotesToTeam() {
  const data = await getJSON('/api/notes-to-team');
  const card = document.getElementById('notesToTeamCard');
  const content = document.getElementById('notesToTeam');
  const hint = document.getElementById('notesToTeamHint');
  content.textContent = data.available ? data.content : '';
  card.hidden = !data.available;
  hint.hidden = !data.truncated;
}
