import React from 'react';

interface Props {
  selected: string;
  onChange: (chartType: string) => void;
}

const ChartSelector: React.FC<Props> = ({ selected, onChange }) => {
  return (
    <div className="mb-4">
      <label htmlFor="chartType" className="mr-2 font-medium">Select chart type:</label>
      <select
        id="chartType"
        value={selected}
        onChange={(e) => onChange(e.target.value)}
        className="border px-2 py-1 rounded"
      >
        <option value="bar">Bar Chart</option>
        <option value="radar">Radar Chart</option>
        <option value="stacked">Stacked Bar Chart</option>
      </select>
    </div>
  );
};

export default ChartSelector;
