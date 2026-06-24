// src/components/FacilitiesTable.js
import React from 'react';

export default function FacilitiesTable({ facilities }) {
  const filterCounts = {
    all: facilities.length,
    dueSoon: facilities.filter(f => f.status === 'due_soon').length,
    upcoming: facilities.filter(f => f.status === 'upcoming').length,
    scheduled: facilities.filter(f => f.status === 'scheduled').length,
    overdue: facilities.filter(f => f.status === 'overdue').length,
    noStatus: facilities.filter(f => f.status === null || f.status === '').length
  };

  return (
    <div className="facilities-container">
      <div className="filters-row">
        <button data-testid="btn-filter-all-facilities">All ({filterCounts.all})</button>
        <button data-testid="btn-filter-due-soon">Due Soon ({filterCounts.dueSoon})</button>
        <button data-testid="btn-filter-upcoming">Upcoming ({filterCounts.upcoming})</button>
        <button data-testid="btn-filter-scheduled">Scheduled ({filterCounts.scheduled})</button>
        <button data-testid="btn-filter-overdue">Overdue ({filterCounts.overdue})</button>
        <button data-testid="btn-filter-no-status">No Status ({filterCounts.noStatus})</button>
      </div>
      <table className="facilities-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Location</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {facilities && facilities.map(f => (
            <tr key={f.id}>
              <td>{f.name}</td>
              <td>{f.location}</td>
              <td>{f.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}